"""Validate the observed ordinary limit-order dialog before one confirmation click."""

import re
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext

from playwright.sync_api import expect

from app.browser.alpha import TABS, verify_identity

MODALS = '[role="dialog"], [aria-modal="true"], .bn-modal'
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


class ConfirmationIncomplete(ValueError):
    """The dialog is visible but required content is still missing."""


def dialog_value(text, label, pattern):
    count = len(re.findall(rf"(?:^|\n)[ \t]*{re.escape(label)}", text))
    if count == 0:
        raise ConfirmationIncomplete(f"确认弹窗的“{label}”尚未加载，未点击继续。")
    if count != 1:
        raise ValueError(f"确认弹窗的“{label}”缺失或不唯一，未点击继续。")
    values = re.findall(
        rf"(?:^|\n)[ \t]*{re.escape(label)}\s*[:：]?\s*({pattern})[ \t]*(?=\n|$)",
        text,
    )
    if not values:
        raise ConfirmationIncomplete(
            f"确认弹窗的“{label}”数值尚未加载或格式不支持，未点击继续。"
        )
    if len(values) != 1:
        raise ValueError(f"确认弹窗的“{label}”缺失、不唯一或格式不支持，未点击继续。")
    return values[0]


def validate_confirmation(text, command):
    text = text.replace("\r\n", "\n").replace("\u00a0", " ")
    if re.search(r"验证码|人机验证|安全验证|风险测评|身份验证|验证器", text):
        raise ValueError("出现验证提示，请手动处理；不会自动点击继续。")
    if not text.strip() or text.strip() in {"加载中", "加载中…"}:
        raise ConfirmationIncomplete("确认弹窗内容尚未加载。")
    if sum(line.strip() == command.expected_symbol for line in text.splitlines()) != 1:
        raise ValueError("确认弹窗的代币名称不匹配，未点击继续。")
    kind = dialog_value(text, "类型", r"限价\s*/\s*(?:买入|卖出)")
    if re.sub(r"\s", "", kind) != "限价/" + TABS[command.side]:
        raise ValueError("确认弹窗的买卖方向不匹配，未点击继续。")

    def amount(label, unit):
        value = dialog_value(text, label, rf"{NUMBER}\s+{re.escape(unit)}")
        return Decimal(value.split()[0].replace(",", ""))

    price = amount("委托价", command.expected_quote)
    quantity = amount("数量", command.expected_symbol)
    gross = amount("成交额", command.expected_quote)
    with localcontext() as context:
        context.prec = 100
        if price != Decimal(command.price) or quantity != Decimal(command.quantity):
            raise ValueError("确认弹窗的委托价或数量与本次指令不一致，未点击继续。")
        expected_gross = price * quantity
        display_unit = Decimal(1).scaleb(gross.as_tuple().exponent)
        displayed_candidates = {
            expected_gross.quantize(display_unit, rounding=ROUND_HALF_UP),
            expected_gross.quantize(display_unit, rounding=ROUND_DOWN),
        }
        if gross not in displayed_candidates:
            raise ValueError("确认弹窗的成交额与委托价、数量不一致，未点击继续。")
        # Alpha may show fees in the purchased token rather than the quote currency.
        fee_labels = [
            label
            for label in ("预计手续费", "预估手续费")
            if re.search(rf"(?:^|\n)[ \t]*{label}", text)
        ]
        if not fee_labels:
            raise ConfirmationIncomplete("确认弹窗的预估手续费尚未加载，未点击继续。")
        if len(fee_labels) != 1:
            raise ValueError("确认弹窗出现多个手续费字段，未点击继续。")
        fee_text = dialog_value(
            text,
            fee_labels[0],
            rf"{NUMBER}\s+(?:{re.escape(command.expected_symbol)}|{re.escape(command.expected_quote)})",
        )
        fee_value, fee_currency = fee_text.split()
        fee = Decimal(fee_value.replace(",", ""))
        quote_fee = fee if fee_currency == command.expected_quote else fee * price
        if command.side == "buy" and gross + quote_fee > Decimal(50):
            raise ValueError("确认弹窗金额含预计手续费超过 50 U，未点击继续。")
    return {
        "price": str(price),
        "quantity": str(quantity),
        "gross": str(gross),
        "estimated_fee": str(fee),
        "fee_currency": fee_currency,
    }


def confirmation_dialogs(page):
    # Deduplicate nested modal wrappers, but reject two separate visible dialogs.
    visible = page.locator(MODALS).filter(visible=True)
    return visible.filter(has_not=page.locator(MODALS).filter(visible=True))


def confirmation_preview(page, payload):
    """Read only; bounded visible dialog text, no form writes or clicks."""
    from app.browser.schemas import FillForm

    command = FillForm(**payload)
    verify_identity(page, command)
    dialogs = confirmation_dialogs(page)
    count = dialogs.count()
    result = {"dialog_count": count, "valid": False, "read_only": True}
    if count != 1:
        result["reason"] = f"识别到 {count} 个可见弹窗，需要唯一订单确认弹窗。"
        buttons = page.get_by_role("button", name="继续", exact=True).filter(
            visible=True
        )
        result["visible_continue_buttons"] = buttons.count()
        if buttons.count() == 1:
            result["button_containers"] = buttons.evaluate("""button => {
              const rows=[];
              let node=button.parentElement;
              for(let i=0;node&&i<5&&node.tagName!=='BODY';i++,node=node.parentElement){
                const text=node.innerText||'';
                rows.push({tag:node.tagName,role:node.getAttribute('role'),
                  classes:String(node.className).slice(0,200),
                  text:text.includes('委托价')&&/预[计估]手续费/.test(text)?text.slice(0,2000):''});
              }
              return rows;
            }""")
        return result
    text = dialogs.inner_text()
    result["dialog_text"] = text[:3000]
    result["continue_button_count"] = dialogs.get_by_role(
        "button", name="继续", exact=True
    ).count()
    try:
        result["values"] = validate_confirmation(text, command)
        result["valid"] = result["continue_button_count"] == 1
        result["reason"] = (
            "确认单字段匹配，未点击任何按钮。"
            if result["valid"]
            else "继续按钮不唯一。"
        )
    except ValueError as exc:
        result["reason"] = str(exc)
    return result


def wait_for_confirmation(page, command, timeout_ms=5000):
    deadline = time.monotonic() + timeout_ms / 1000
    last_error = "未识别到可见订单确认弹窗。"
    while time.monotonic() < deadline:
        dialogs = confirmation_dialogs(page)
        count = dialogs.count()
        if count > 1:
            raise ValueError("出现多个可见弹窗，未点击继续。")
        if count == 1:
            try:
                validate_confirmation(dialogs.inner_text(), command)
                return dialogs
            except ConfirmationIncomplete as exc:
                last_error = str(exc)
        page.wait_for_timeout(100)
    raise ValueError(f"等待订单确认弹窗内容超时：{last_error}")


def confirm_once(page, command, deadline=None):
    deadline_at = time.monotonic() + 5
    while time.monotonic() < deadline_at:
        dialogs = confirmation_dialogs(page)
        if dialogs.count() > 1:
            raise ValueError("出现多个可见弹窗，未点击继续。")
        if dialogs.count() == 1:
            text = dialogs.inner_text()
            if re.search(r"验证码|人机验证|安全验证|风险测评|身份验证|验证器", text):
                raise ValueError("出现验证提示，请手动处理；不会自动点击继续。")
            button = dialogs.get_by_role("button", name="继续", exact=True)
            if button.count() == 1:
                expect(button).to_be_enabled(timeout=5000)
                button.click(trial=True, timeout=3000)
                button.click(timeout=3000)  # Never retry this click, including on timeout.
                return {"unchecked_confirmation": True}
        page.wait_for_timeout(100)
    raise ValueError("等待普通订单确认弹窗或唯一“继续”按钮超时。")
