import json
import re
import asyncio
import traceback
from typing import Optional, List, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import (
    UserMessageSegment,
    AssistantMessageSegment,
    SystemMessageSegment,
    TextPart,
)


class ConversationRewriter(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        logger.info("会话修改插件已加载")

    async def terminate(self):
        logger.info("会话修改插件已卸载")

    @filter.command("rewrite")
    async def rewrite(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("[FAIL] 本插件仅支持私聊使用，群聊暂不支持")
            logger.info("[rewrite] 已拦截群聊中的命令请求")
            return

        raw = event.message_str.strip()
        raw = re.sub(r'\s*\[MSG_ID:\d+\]$', '', raw)
        logger.info(f"[rewrite] 清洗后: {repr(raw)}")

        target, old, new, err = self._parse_args(raw)
        if err:
            logger.info(f"[rewrite] 解析失败: {err}")
            yield event.plain_result(f"[FAIL] {err}")
            return
        logger.info(f"[rewrite] target={target}, old={repr(old)}, new={repr(new)}")

        if target == "ai" and not self.config.get("allow_modify_assistant", True):
            yield event.plain_result("[FAIL] 修改 AI 记忆功能未开启")
            return

        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager

        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        except Exception:
            logger.error(f"获取对话ID失败: {traceback.format_exc()}")
            yield event.plain_result("[FAIL] 获取对话 ID 失败")
            return
        if not curr_cid:
            yield event.plain_result("[FAIL] 无法获取当前对话 ID")
            return

        try:
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
        except Exception:
            logger.error(f"获取对话对象失败: {traceback.format_exc()}")
            yield event.plain_result("[FAIL] 获取对话对象失败")
            return
        if not conversation or not conversation.history:
            yield event.plain_result("[FAIL] 当前会话没有可修改的对话历史")
            return

        history = self._load_history(conversation.history)
        if not history:
            yield event.plain_result("[FAIL] 对话历史格式异常")
            return

        if target == "user":
            yield await self._handle_user_rewrite(event, history, old, new, umo, curr_cid)
        else:
            yield await self._handle_ai_rewrite(event, history, old, new, umo, curr_cid)

    async def _handle_user_rewrite(
        self, event: AstrMessageEvent,
        history: List[dict],
        old: str, new: str,
        umo: str, curr_cid: str
    ):
        # 1. 定位最后一条用户消息
        last_user_idx, last_user = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            return event.plain_result("[FAIL] 没有找到用户消息")

        user_text, system_items = self._split_content(last_user.get("content"))
        if old not in user_text:
            return event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
        if user_text.count(old) > 1:
            return event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")

        # 2. 构造新用户消息
        new_user_text = user_text.replace(old, new, 1)
        new_content = system_items + [{"type": "text", "text": new_user_text}]
        new_user_msg = {"role": "user", "content": new_content}

        # 3. 确定删除区间（确保 end >= start）
        last_ai_idx, _ = self._find_last_by_role(history, "assistant")
        delete_start = last_user_idx
        delete_end = max(last_ai_idx, last_user_idx)

        temp_history = [msg for i, msg in enumerate(history) if i < delete_start or i > delete_end]
        temp_history.append(new_user_msg)

        # 4. 加载人格 system_prompt
        system_prompt = ""
        try:
            conv_mgr = self.context.conversation_manager
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            persona_id = conversation.persona_id if conversation else None
            if persona_id:
                persona = self.context.persona_manager.get_persona(persona_id)
                if persona:
                    system_prompt = persona.system_prompt
                    logger.info(f"[rewrite] 已加载人格: {persona_id}, 长度: {len(system_prompt)}")
                else:
                    logger.warning(f"[rewrite] 人格未找到: {persona_id}")
            else:
                default_persona = await self.context.persona_manager.get_default_persona_v3(umo)
                if default_persona and "prompt" in default_persona:
                    system_prompt = default_persona["prompt"]
                    logger.info("[rewrite] 使用默认人格")
        except Exception:
            logger.error(f"加载人格失败: {traceback.format_exc()}")

        # 5. 构建 LLM 上下文
        contexts = []
        if system_prompt:
            contexts.append(SystemMessageSegment(content=[TextPart(text=system_prompt)]))
        contexts.extend(self._history_to_message_segments(temp_history))

        # 6. 调用 LLM
        try:
            prov_id = await self.context.get_current_chat_provider_id(umo)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=contexts,
            )
            assistant_text = llm_resp.completion_text
        except asyncio.TimeoutError:
            logger.error("LLM 调用超时")
            return event.plain_result("[FAIL] 调用 LLM 超时，历史未修改")
        except Exception:
            logger.error(f"LLM 调用失败: {traceback.format_exc()}")
            return event.plain_result("[FAIL] 调用 LLM 失败，历史未修改")

        # 7. 处理并发新消息
        try:
            fresh_conv = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            if fresh_conv and fresh_conv.history:
                fresh_history = self._load_history(fresh_conv.history)
                extra_msgs = fresh_history[len(history):] if len(fresh_history) > len(history) else []
            else:
                extra_msgs = []
        except Exception:
            logger.error(f"并发检查失败: {traceback.format_exc()}")
            extra_msgs = []

        # 8. 安全修改历史（重要：先删除旧区间，再追加本次重写的新对话，最后追加并发消息）
        self._remove_range(history, delete_start, delete_end)
        # 先添加本次重写产生的新用户消息和 AI 回复
        history.append(new_user_msg)
        history.append({"role": "assistant", "content": [{"type": "text", "text": assistant_text}]})
        # 再追加 LLM 期间并发的新消息（它们应该发生在本次重写之后）
        if extra_msgs:
            history.extend(extra_msgs)
            logger.info(f"[rewrite] 追加了 {len(extra_msgs)} 条并发消息（已保证时间顺序）")

        # 9. 持久化
        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] 新对话对已保存")
        except Exception:
            logger.error(f"保存对话失败: {traceback.format_exc()}")
            return event.plain_result("[FAIL] 修改成功但保存失败")

        return event.chain_result([Plain(assistant_text)])

    async def _handle_ai_rewrite(self, event, history, old, new, umo, curr_cid):
        last_ai_idx, last_ai = self._find_last_by_role(history, "assistant")
        if last_ai_idx == -1:
            return event.plain_result("[FAIL] 没有找到 AI 回复")
        ai_text, _ = self._split_content(last_ai.get("content"))
        if old not in ai_text:
            return event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
        if ai_text.count(old) > 1:
            return event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")
        new_ai_text = ai_text.replace(old, new, 1)
        last_ai["content"] = [{"type": "text", "text": new_ai_text}]

        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] AI 记忆已更新")
        except Exception:
            logger.error(f"保存 AI 记忆失败: {traceback.format_exc()}")
            return event.plain_result("[FAIL] 保存失败")
        return event.plain_result("[OK] AI 记忆已修正，下次对话将基于新记忆")

    @filter.command("rewrite_help")
    async def rewrite_help(self, event: AstrMessageEvent):
        msg = (
            "【会话修改插件】会话修改插件帮助\n"
            "修改自己最后一条消息：/rewrite user \"旧文本\" \"新文本\"\n"
            "修改 AI 最后一条回复：/rewrite ai \"旧文本\" \"新文本\"\n"
            "支持子串替换，若重复多处会提示；文本含空格请用引号或括号包裹。\n"
            "支持的包裹符号：英文双引号、英文单引号、半角圆括号、中文单双引号"
        )
        yield event.plain_result(msg)

    # ---------- 工具函数（保持不变） ----------
    @staticmethod
    def _split_content(content) -> Tuple[str, List[dict]]:
        if isinstance(content, str):
            return re.sub(r'\s*\[MSG_ID:\d+\]$', '', content).strip(), []
        if isinstance(content, list):
            user_parts = []
            system_items = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = item.get("text", "")
                    if raw.startswith("<system_reminder"):
                        system_items.append(item)
                    else:
                        user_parts.append(re.sub(r'\s*\[MSG_ID:\d+\]$', '', raw))
                else:
                    system_items.append(item)
            return "".join(user_parts).strip(), system_items
        return str(content).strip(), []

    @staticmethod
    def _parse_args(raw: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        raw = re.sub(r'\s*\[MSG_ID:\d+\]$', '', raw).strip()
        match = re.match(r'^[^\w]?rewrite\s+(user|ai)\s+(.*)', raw, re.IGNORECASE)
        if not match:
            return None, None, None, "指令格式错误，应为 rewrite user/ai <旧文本> <新文本>"
        target = match.group(1).lower()
        args_str = match.group(2).strip()

        delim_pairs = [('"', '"'), ("'", "'"), ('(', ')'), ('\u201c', '\u201d'), ('\u2018', '\u2019')]
        for start, end in delim_pairs:
            if args_str.startswith(start):
                end_idx = args_str.find(end, 1)
                if end_idx == -1:
                    return None, None, None, f"未找到匹配的结束符号: {repr(end)}"
                old = args_str[1:end_idx]
                remaining = args_str[end_idx + 1:].strip()
                if not remaining.startswith(start):
                    return None, None, None, "两个参数必须使用相同的包裹符号"
                end_idx2 = remaining.find(end, 1)
                if end_idx2 == -1:
                    return None, None, None, "第二个参数未找到匹配的结束符号"
                new = remaining[1:end_idx2]
                if remaining[end_idx2 + 1:].strip():
                    return None, None, None, "参数数量过多"
                return target, old, new, None

        tokens = args_str.split()
        if len(tokens) != 2:
            return None, None, None, "需要两个参数（若文本含空格，请使用引号或括号包裹）"
        return target, tokens[0], tokens[1], None

    @staticmethod
    def _load_history(history_raw) -> List[dict]:
        if isinstance(history_raw, list):
            return list(history_raw)
        if isinstance(history_raw, str):
            try:
                data = json.loads(history_raw)
            except json.JSONDecodeError:
                logger.error("历史记录 JSON 解析失败")
                return []
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    logger.error("二次 JSON 解析失败")
                    return []
            if isinstance(data, list):
                return list(data)
        return []

    @staticmethod
    def _find_last_by_role(history: List[dict], role: str) -> Tuple[int, Optional[dict]]:
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if isinstance(msg, dict) and msg.get("role") == role:
                return i, msg
        return -1, None

    @staticmethod
    def _remove_range(history: List[dict], start: int, end: int):
        if 0 <= start <= end < len(history):
            del history[start:end + 1]
        else:
            logger.warning(f"_remove_range 索引无效: start={start}, end={end}, len={len(history)}")

    @staticmethod
    def _history_to_message_segments(history: List[dict]) -> List:
        segments = []
        for msg in history:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                non_text_count = 0
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    else:
                        non_text_count += 1
                if non_text_count > 0:
                    logger.warning(f"消息包含 {non_text_count} 个非文本组件，将被丢弃")
                text = "".join(text_parts)
            else:
                text = str(content)

            role = msg.get("role")
            if role == "user":
                segments.append(UserMessageSegment(content=[TextPart(text=text)]))
            elif role == "assistant":
                segments.append(AssistantMessageSegment(content=[TextPart(text=text)]))
            elif role == "system":
                segments.append(SystemMessageSegment(content=[TextPart(text=text)]))
        return segments
