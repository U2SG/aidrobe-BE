"""Read a design conversation's saved packing list without generating a new one."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.packing.generate import describe_packing_list, load_conversation_packing_list
from app.common.locale import lang_subtag

_LIST_NAMES = ("打包清单", "打包单", "行李清单", "packing list", "packing plan")
_SAVED_REFERENCES = ("刚才", "刚保存", "已保存", "这份", "刚生成", "saved", "actual", "existing")
_READ_REQUESTS = ("读", "看", "列", "哪些", "什么", "read", "show", "tell", "what", "which")
_EDIT_REQUESTS = (
    "换成", "换掉", "帮我换", "替换", "调整", "删除", "去掉", "加上", "重新排", "重排", "重新生成",
    "replace ", "swap ", "change ", "remove ", "add ", "update ", "redo ",
)


async def read_saved_packing_answer(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, question: str, locale: str,
) -> str | None:
    text = question.casefold()
    refers_to_list = any(name in text for name in _LIST_NAMES) or (
        "清单" in text and any(reference in text for reference in _SAVED_REFERENCES)
    )
    if (
        not refers_to_list or not any(request in text for request in _READ_REQUESTS)
        or any(request in text for request in _EDIT_REQUESTS)
    ):
        return None
    packing = await load_conversation_packing_list(session, user_id, conversation_id)
    if packing is None:
        return None
    facts = await describe_packing_list(session, packing, locale)
    english = lang_subtag(locale) == "en"
    days = facts["days"]
    destination = facts["destination"]
    if english:
        heading = f"Saved packing list for {destination} ({days} days)." if destination else (
            f"Saved packing list ({days} days)."
        )
    else:
        heading = f"已保存的{destination or ''}打包清单，共 {days} 天。"
    names_by_day = {day["day"]: day["items"] for day in facts["daily_outfits"]}
    lines = [heading]
    missing = "Item no longer in wardrobe" if english else "已不在衣橱里的一件"
    empty = "No items saved" if english else "尚无已保存的单品"
    for day in range(1, days + 1):
        names = [item["name"] or missing for item in names_by_day.get(day, [])]
        if english:
            lines.append(f"Day {day}: {', '.join(names) or empty}")
        else:
            lines.append(f"第 {day} 天：{'、'.join(names) or empty}")
    return "\n".join(lines)
