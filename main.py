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
        """主入口，仅限私聊，分发到 user/ai 处理"""
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

        # 获取对话历史
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
        """处理修改用户消息的逻辑"""
        # 查找最后一条用户消息的索引和内容
        last_user_idx, last_user = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            return event.plain_result("[FAIL] 没有找到用户消息")

        user_text, system_items = self._split_content(last_user.get("content"))
        if old not in user_text:
            return event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
        count = user_text.count(old)
        if count > 1:
            return event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")

        # 构造新用户消息（不修改原历史）
        new_user_text = user_text.replace(old, new, 1)
        new_content = system_items + [{"type": "text", "text": new_user_text}]
        new_user_msg = {"role": "user", "content": new_content}

        # 暂时在历史末尾追加新消息用于生成（不删除旧消息）
        temp_history = history + [new_user_msg]
        contexts = self._history_to_message_segments(temp_history)

        # 先调用 LLM，成功后再修改历史
        try:
            prov_id = await self.context.get_current_chat_provider_id(umo)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=contexts,
            )
            assistant_text = llm_resp.completion_text
        except Exception:
            logger.error(traceback.format_exc())
            return event.plain_result("[FAIL] 调用 LLM 失败，历史未修改")

        # LLM 成功，现在安全地删除旧对话对，添加新 user 和 assistant
        last_ai_idx, _ = self._find_last_by_role(history, "assistant")
        self._remove_messages_by_indices(history, last_user_idx, last_ai_idx if last_ai_idx != -1 else None)
        history.append(new_user_msg)
        assistant_content = [{"type": "text", "text": assistant_text}]
        history.append({"role": "assistant", "content": assistant_content})

        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] 新对话对已保存")
        except Exception:
            logger.error(traceback.format_exc())
            return event.plain_result("[FAIL] 修改成功但保存失败")

        return event.chain_result([Plain(assistant_text)])

    async def _handle_ai_rewrite(
        self, event: AstrMessageEvent,
        history: List[dict],
        old: str, new: str,
        umo: str, curr_cid: str
    ):
        """处理修改 AI 记忆的逻辑"""
        last_ai_idx, last_ai = self._find_last_by_role(history, "assistant")
        if last_ai_idx == -1:
            return event.plain_result("[FAIL] 没有找到 AI 回复")

        ai_text, _ = self._split_content(last_ai.get("content"))
        if old not in ai_text:
            return event.plain_result("[FAIL] 未找到匹配的原文，请检查后重试")
        count = ai_text.count(old)
        if count > 1:
            return event.plain_result("[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")

        new_ai_text = ai_text.replace(old, new, 1)
        last_ai["content"] = [{"type": "text", "text": new_ai_text}]

        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] AI 记忆已更新")
        except Exception:
            logger.error(traceback.format_exc())
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

    # ---------- 静态工具方法 ----------

    @staticmethod
    def _split_content(content) -> Tuple[str, List[dict]]:
        """从 content 中分离用户可见文本和系统标签"""
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
        """解析 /rewrite 参数，返回 (target, old, new, error)"""
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
        """安全加载历史记录，返回浅拷贝以避免外部引用污染"""
        if isinstance(history_raw, list):
            return list(history_raw)  # 浅拷贝
        if isinstance(history_raw, str):
            try:
                data = json.loads(history_raw)
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, list):
                    return list(data)
            except Exception:
                pass
        return []

    @staticmethod
    def _find_last_by_role(history: List[dict], role: str) -> Tuple[int, Optional[dict]]:
        """倒序查找最后一条指定角色的消息，返回 (索引, 消息) 或 (-1, None)"""
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if isinstance(msg, dict) and msg.get("role") == role:
                return i, msg
        return -1, None

    @staticmethod
    def _remove_messages_by_indices(history: List[dict], idx1: int, idx2: Optional[int]):
        """根据索引删除历史中的消息（先删除大索引避免错位）"""
        indices = sorted([idx1, idx2] if idx2 is not None else [idx1], reverse=True)
        for i in indices:
            if 0 <= i < len(history):
                del history[i]

    @staticmethod
    def _history_to_message_segments(history: List[dict]) -> List:
        """将 dict 历史转换为 LLM 上下文段"""
        segments = []
        for msg in history:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                text = str(content)
            if msg["role"] == "user":
                segments.append(UserMessageSegment(content=[TextPart(text=text)]))
            elif msg["role"] == "assistant":
                segments.append(AssistantMessageSegment(content=[TextPart(text=text)]))
        return segments
