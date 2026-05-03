import json
import re
import traceback
from typing import Dict, Optional, List, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import (
    UserMessageSegment,
    AssistantMessageSegment,
    TextPart,
)


class ConversationRewriter(Star):
    """
    会话修改插件
    /rewrite user <旧文本> <新文本>  → 修改最后一条自己发送的消息，AI重新回答
    /rewrite ai   <旧文本> <新文本>  → 修改最后一条AI记忆
    支持英文/中文引号、半角括号包裹含空格文本。
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        logger.info("会话修改插件已加载")

    async def terminate(self):
        logger.info("会话修改插件已卸载")


    @filter.command("rewrite")
    async def rewrite(self, event: AstrMessageEvent):
        # 限于多种群聊增强插件的干扰，限制在群聊中使用
        if event.get_group_id():
            yield event.plain_result("[FAIL] 本插件仅支持私聊使用，群聊暂不支持")
            logger.info(f"[rewrite]已拦截群聊中的命令请求")
            return
        
        # 正常逻辑
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
            logger.error(traceback.format_exc())
            yield event.plain_result("[FAIL] 获取对话 ID 失败")
            return
        if not curr_cid:
            yield event.plain_result("[FAIL] 无法获取当前对话 ID")
            return

        try:
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
        except Exception:
            logger.error(traceback.format_exc())
            yield event.plain_result("[FAIL] 获取对话对象失败")
            return
        if not conversation or not conversation.history:
            yield event.plain_result("[FAIL] 当前会话没有可修改的对话历史")
            return

        history = self._load_history(conversation.history)
        if not history:
            yield event.plain_result("[FAIL] 对话历史格式异常")
            return

        last_user = self._find_last_by_role(history, "user")
        last_ai = self._find_last_by_role(history, "assistant")
        if not last_user:
            yield event.plain_result("[FAIL] 没有找到用户消息")
            return

        if target == "user":
            # 提取用户可见文本
            user_text, system_items = self._split_content(last_user.get("content"))
            if old not in user_text:
                yield event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
                return
            count = user_text.count(old)
            if count > 1:
                yield event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")
                return

            # 子串替换
            new_user_text = user_text.replace(old, new, 1)
            # 构建新的 content：保留 system_items，添加新用户文本
            new_content = system_items + [{"type": "text", "text": new_user_text}]
            new_user_msg = {"role": "user", "content": new_content}

            # 删除旧对话对
            self._remove_last_pair(history, last_user, last_ai)
            history.append(new_user_msg)
            logger.info("[rewrite] 已删除旧对话对，新 user 消息加入历史")

            # 调用 LLM 生成回复
            try:
                prov_id = await self.context.get_current_chat_provider_id(umo)
                contexts = self._history_to_message_segments(history)
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id,
                    contexts=contexts,
                )
                assistant_text = llm_resp.completion_text
            except Exception:
                logger.error(traceback.format_exc())
                # 保存一条占位 assistant 消息，维持对话结构完整
                history.append({"role": "assistant", "content": [{"type": "text", "text": "<system_reminder>回复生成失败</system_reminder>"}]})
                try:
                    await conv_mgr.update_conversation(umo, curr_cid, history=history)
                    logger.info("[rewrite] 错误占位消息已保存")
                except Exception as save_err:
                    logger.error(f"保存错误占位消息失败: {save_err}")
                yield event.plain_result("[FAIL] 调用 LLM 失败，已填充预设回复")
                return

            # 构造 assistant content（纯文本）
            assistant_content = [{"type": "text", "text": assistant_text}]
            history.append({"role": "assistant", "content": assistant_content})
            try:
                await conv_mgr.update_conversation(umo, curr_cid, history=history)
                logger.info("[rewrite] 新对话对已保存")
            except Exception:
                logger.error(traceback.format_exc())
                yield event.plain_result("[FAIL] 修改成功但保存失败")
                return

            yield event.chain_result([Plain(assistant_text)])

        else:  # target == "ai"
            if not last_ai:
                yield event.plain_result("[FAIL] 没有找到 AI 回复")
                return

            ai_text, _ = self._split_content(last_ai.get("content"))
            if old not in ai_text:
                yield event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
                return
            count = ai_text.count(old)
            if count > 1:
                yield event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")
                return

            # 子串替换
            new_ai_text = ai_text.replace(old, new, 1)
            # 构建新 content（保留可能存在的其他组件，简单处理：只用纯文本）
            last_ai["content"] = [{"type": "text", "text": new_ai_text}]

            try:
                await conv_mgr.update_conversation(umo, curr_cid, history=history)
                logger.info("[rewrite] AI 记忆已更新")
            except Exception:
                logger.error(traceback.format_exc())
                yield event.plain_result("[FAIL] 保存失败")
                return
            yield event.plain_result("[OK] AI 记忆已修正，下次对话将基于新记忆")

    @filter.command("rewrite_help")
    async def rewrite_help(self, event: AstrMessageEvent):
        msg = (
            "【会话修改插件】会话修改插件帮助\n"
            "修改自己最后一条消息：/rewrite user \"旧文本\" \"新文本\"\n"
            "修改 AI 最后一条回复：/rewrite ai \"旧文本\" \"新文本\"\n"
            "支持子串替换，若重复多处会提示；文本含空格请用引号或括号包裹。\n"
            "此外，除了使用双引号包裹，还支持：\n"
            "英文双引号、英文单引号、半角圆括号、中文单引号"
        )
        yield event.plain_result(msg)
        
    # ----------------- 工具函数 -----------------

    @staticmethod
    def _split_content(content) -> Tuple[str, List[dict]]:
        """
        从 content 列表中分离用户可见文本和系统标签。
        返回 (user_text, system_items)
        """
        if isinstance(content, str):
            text = re.sub(r'\s*\[MSG_ID:\d+\]$', '', content).strip()
            return text, []
        if isinstance(content, list):
            user_parts = []
            system_items = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = item.get("text", "")
                    if raw.startswith("<system_reminder"):
                        system_items.append(item)  # 保留原样
                    else:
                        cleaned = re.sub(r'\s*\[MSG_ID:\d+\]$', '', raw)
                        user_parts.append(cleaned)
                else:
                    # 非 text 类型也保留
                    system_items.append(item)
            return "".join(user_parts).strip(), system_items
        return str(content).strip(), []

    @staticmethod
    def _parse_args(raw: str):
        raw = re.sub(r'\s*\[MSG_ID:\d+\]$', '', raw).strip()
        if raw.startswith("/rewrite"):
            raw = raw[len("/rewrite"):].strip()
        elif raw.startswith("rewrite"):
            raw = raw[len("rewrite"):].strip()
        else:
            return None, None, None, "指令格式错误，应为 /rewrite user/ai <旧文本> <新文本>"
        if not raw:
            return None, None, None, "缺少参数"
        parts = raw.split(maxsplit=1)
        target = parts[0].lower()
        if target not in ("user", "ai"):
            return None, None, None, f"目标角色必须是 'user' 或 'ai'，而不是 '{target}'"
        if len(parts) == 1:
            return None, None, None, "缺少旧文本和新文本"
        args_str = parts[1].strip()
        delim_pairs = [('"','"'), ("'","'"), ('(',')'), ('“','”'), ('‘','’')]
        for start, end in delim_pairs:
            if args_str.startswith(start):
                end_idx = args_str.find(end, 1)
                if end_idx == -1:
                    return None, None, None, f"未找到匹配的结束符号: {repr(end)}"
                old = args_str[1:end_idx]
                remaining = args_str[end_idx+1:].strip()
                if not remaining.startswith(start):
                    return None, None, None, "两个参数必须使用相同的包裹符号"
                end_idx2 = remaining.find(end, 1)
                if end_idx2 == -1:
                    return None, None, None, "第二个参数未找到匹配的结束符号"
                new = remaining[1:end_idx2]
                leftover = remaining[end_idx2+1:].strip()
                if leftover:
                    return None, None, None, "参数数量过多"
                return target, old, new, None
        tokens = args_str.split()
        if len(tokens) != 2:
            return None, None, None, "需要两个参数（若文本含空格，请使用引号或括号包裹）"
        return target, tokens[0], tokens[1], None

    @staticmethod
    def _load_history(history_raw) -> list:
        if isinstance(history_raw, list):
            return history_raw
        if isinstance(history_raw, str):
            try:
                data = json.loads(history_raw)
                if isinstance(data, str):
                    data = json.loads(data)
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    @staticmethod
    def _find_last_by_role(history: list, role: str) -> Optional[dict]:
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == role:
                return msg
        return None

    @staticmethod
    def _remove_last_pair(history: list, last_user: dict, last_ai: dict):
        if last_user in history:
            history.remove(last_user)
        if last_ai and last_ai in history:
            history.remove(last_ai)

    @staticmethod
    def _history_to_message_segments(history: List[dict]) -> List:
        segments = []
        for msg in history:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                text = str(content)
            if msg["role"] == "user":
                segments.append(UserMessageSegment(content=[TextPart(text=text)]))
            elif msg["role"] == "assistant":
                segments.append(AssistantMessageSegment(content=[TextPart(text=text)]))
        return segments
