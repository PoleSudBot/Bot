import asyncio
import json
import random
import re
from typing import Dict, List

import nonebot_plugin_localstore as store

from zhenxun.services.ai import LLMMessage, generate
from zhenxun.services.log import logger

from .config import get_ai_enabled, get_llm_model_name

# 数据文件
ROAST_LIB_FILE = store.get_plugin_data_file("roast_library.json")

# ================= 默认兜底文案模板 =================
DEFAULT_TEMPLATES = [
    "你本是一只无忧无虑的【{origin}】，却没能逃过命运的安排，含泪变成了【{food}】。",
    "看看你现在的样子！虽然不再是【{origin}】，但作为【{food}】的你，依然散发着诱人的光泽。",
]

BURNT_TEMPLATES = [
    "住手！它已经是一块【{origin}】了！在你无情的二次烧烤下，它彻底变成了黑漆漆的焦炭。",
    "你还不满足吗？这块可怜的【{origin}】已经被你烤得面目全非，化作了尘埃。",
]

PVP_TEMPLATES = [
    "{k}手法娴熟，手起刀落，把{v}做成了美味的【{food}】！",
    "{v}还没反应过来，就被{k}扔上了烤架。再见了，{origin}；你好，{food}。",
]


class RoastManager:
    def __init__(self):
        self.file = ROAST_LIB_FILE
        self.library: Dict[str, Dict[str, List[str]]] = self._load()
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        if not self.file.exists():
            return {}
        try:
            return json.loads(self.file.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"roast_library.json 读取失败，已使用空文案库兜底: {e}")
            return {}

    def _sync_save(self):
        """用临时文件原子替换文案库，避免生成中断留下半截 JSON。"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(f"{self.file.name}.{id(self)}.tmp")
        tmp.write_text(
            json.dumps(self.library, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.file)

    async def _save_new_text(self, origin_id: str, target_id: str, text: str):
        normalized_text = self._normalize_pvp_placeholder_spacing(text)
        async with self._lock:
            if origin_id not in self.library:
                self.library[origin_id] = {}
            if target_id not in self.library[origin_id]:
                self.library[origin_id][target_id] = []
            if normalized_text not in self.library[origin_id][target_id]:
                self.library[origin_id][target_id].append(normalized_text)
                await asyncio.to_thread(self._sync_save)

    def _normalize_pvp_placeholder_spacing(self, text: str) -> str:
        # 历史 AI 文案会把 {k}/{v} 两侧补空格，名字改成「昵称」后在图片里会显得断开。
        return re.sub(r"[ \u3000]*\{([kv])\}[ \u3000]*", r"{\1}", text)

    def _format_text(
        self,
        text: str,
        origin: str,
        food: str,
        killer: str | None = None,
        victim: str | None = None,
    ) -> str:
        res = self._normalize_pvp_placeholder_spacing(text)
        res = res.replace("{origin}", origin).replace("{food}", food)
        k_name = killer if killer else "神秘人"
        v_name = victim if victim else "倒霉蛋"
        res = res.replace("{k}", k_name).replace("{v}", v_name)
        return res

    async def get_roast_text(
        self,
        origin_pig: dict,
        target_food: dict,
        operator_name: str | None = None,
        target_name: str | None = None,
    ) -> str:
        o_id = origin_pig["id"]
        t_id = target_food["id"]
        o_name = origin_pig["name"]
        t_name = target_food["name"]

        if t_id == "burnt":
            if get_ai_enabled():
                try:
                    text = await self._call_ai(origin_pig, target_food, is_burnt=True)
                    return self._format_text(text, o_name, t_name)
                except Exception as e:
                    logger.warning(f"焦炭文案 AI 生成失败，回落本地模板: {e}")
            return random.choice(BURNT_TEMPLATES).format(origin=o_name)

        lookup_t_id = t_id + ("_pvp" if operator_name else "")
        local_texts = self.library.get(o_id, {}).get(lookup_t_id, [])

        should_generate = get_ai_enabled() and (
            (not local_texts) or (len(local_texts) < 3 and random.random() < 0.4)
        )

        template_text = None
        if should_generate:
            try:
                template_text = await self._call_ai(
                    origin_pig,
                    target_food,
                    is_pvp=bool(operator_name),
                )
                if template_text:
                    await self._save_new_text(o_id, lookup_t_id, template_text)
            except Exception as e:
                logger.warning(f"AI 生成失败，回落本地文案: {e}")

        if not template_text and local_texts:
            template_text = random.choice(local_texts)

        if not template_text:
            template_text = (
                random.choice(PVP_TEMPLATES)
                if operator_name
                else random.choice(DEFAULT_TEMPLATES)
            )

        return self._format_text(
            template_text,
            o_name,
            t_name,
            operator_name,
            target_name,
        )

    async def _call_ai(
        self,
        origin_pig: dict,
        target_food: dict,
        is_pvp: bool = False,
        is_burnt: bool = False,
    ) -> str:
        origin_feature = origin_pig.get("description", "")
        if not origin_feature or len(origin_feature) > 15:
            origin_feature = origin_pig.get("analysis", "")[:20]

        system_prompt = "你是一个擅长黑色幽默、说话刻薄但好笑的脱口秀演员。你的任务是进行‘猪生终结’吐槽。"

        if is_burnt:
            prompt = (
                f"【吐槽对象】：一块已经是美食的【{origin_pig['name']}】，被贪婪的人类再次放上烤架，彻底烤成了【焦炭/致癌物】。\n"
                "请写一段40字以内的毒舌吐槽。\n\n"
                "严格遵守【对比公式】：\n"
                "“曾经你(美食状态)...如今你(焦炭状态)...”\n\n"
                "参考范例：\n"
                "“曾经你是鲜嫩多汁的培根，如今却变成了一块用来画眉毛的木炭。人类的贪婪真是你的火葬场。”\n"
                "要求：风格地狱笑话，尖酸刻薄，严禁客套。"
            )
        elif is_pvp:
            prompt = (
                f"【吐槽对象】：凶手把受害者（本体【{origin_pig['name']}】，特征：{origin_feature}）残忍地做成了【{target_food['name']}】。\n"
                "请写一段40字以内的解说，必须使用占位符：{k}代表凶手，{v}代表受害者。\n\n"
                "严格遵守【对比公式】：\n"
                "“{k} (动作)... 把 {v} (惨状/前世特征)... 变成了 (今生美食)...”\n\n"
                "参考范例：\n"
                "“{k} 没给 {v} 任何辩解的机会。前一秒它还是只特立独行的野猪，下一秒就成了 {k} 盘子里滋滋作响的五花肉。”\n"
                "“{k} 的手艺真是‘惊天地泣鬼神’，硬生生把 {v} 这只大懒猪，炼成了一锅香喷喷的猪油。”\n"
                "要求：既要体现受害者惨状，又要调侃凶手，必须包含 {k} 和 {v}。"
            )
        else:
            prompt = (
                "现在进行一场【猪生终结吐槽大会】。\n"
                f"对象前世：【{origin_pig['name']}】（特征：{origin_feature}）\n"
                f"对象今生：【{target_food['name']}】\n\n"
                "请写一段40字以内的神吐槽。必须严格遵守以下【对比公式】：\n"
                "“曾经你(前世特征/地位)...如今你(死后状态/口感)...”\n\n"
                "参考范例（学习这种语气）：\n"
                "“曾经你是丛林里的一方霸主野猪，如今却成为培根在我的平底锅里滋滋作响。别说，比起你的獠牙，还是你的油脂更迷人。”\n"
                "“生前你是个除了吃就是睡的大懒猪，没想到变成红烧肉后，这层肥膘反而成了精华，真是懒猪有懒福。”\n\n"
                "要求：\n"
                "1. 必须同时提到“生前”和“死后”的反差。\n"
                "2. 风格要毒舌、幽默、带点地狱笑话，不要纯夸好吃。\n"
                "3. 严禁出现“这道菜”“这道美食”这类客套话，直接对话（用“你”）。"
            )

        response = await generate(
            [
                LLMMessage.system(system_prompt),
                LLMMessage.user(prompt),
            ],
            model=get_llm_model_name(),
        )
        content = response.text
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI empty response")
        return content.strip().strip('"').strip("'").replace("\n", "")


roast_manager = RoastManager()
