"""Read and navigate the signed-in JD Finance desktop app on Windows."""
import ctypes
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PureWindowsPath
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .jd_live_readonly import PRODUCTS, age, enabled_banks, number, record_failure, record_success

try:
    from _ctypes import COMError
except ImportError:
    UI_READ_ERRORS = ()
else:
    UI_READ_ERRORS = (COMError,)

WINDOW_NAME = "京东金融"
SOURCE = "desktop"
PROVIDER = "https://dingpan.jd.com"
PRODUCT_PATH = "/finance-gold/gold-standard/home/"
DISPLAY_PRICE = {"zheshang": "浙商实时金价", "minsheng": "民生实时金价"}
PAYMENT_BANK = {"zheshang": "浙商银行", "minsheng": "民生银行"}
CASH_SOURCE = "jd_desktop_gold_account"
ORDERS_PATH = "/fg/limit-trade/home/"
MARKET_PANELS = {
    "market_buy": ("买入", "bottom-view-wrap-btngroup__btn--buy", "立即买入"),
    "market_sell": ("卖出", "bottom-view-wrap-btngroup__btn--sell", "立即卖出"),
}
BUY_UNIT_MARKERS = ("买入金额", "买入克重")
MARKET_CLOSE_CONTROLS = {
    "market_buy": (BUY_UNIT_MARKERS, "cf-gold-sd-buy-index__close"),
    "market_sell": (("卖出克重(克)",), "cf-gold-citic-sell-index__close"),
}
GRAM_INPUTS = {
    "market_buy": ("cf-gold-sd-buy-index-money__input", "gold-sd-buy"),
    "market_sell": ("cf-gold-citic-sell-index-money__input", "gold-sd-sell"),
}
BUY_GRAM_RULE = re.compile(r"([0-9]+(?:\.[0-9]+)?)克起购，([0-9]+(?:\.[0-9]+)?)克递增")
SELL_WHOLE_RULE = re.compile(r"持仓小于等于([0-9]+(?:\.[0-9]+)?)克需全部卖出")
SELL_COUPON_ROW = "cf-gold-citic-sell-index-coupon"
SELL_COUPON_POPUP = f"{SELL_COUPON_ROW}-popup"
FEE_DISCOUNT = re.compile(r"[0-9](?:\.\d+)?折")
ORDER_TABS = {
    "limit_buy": "限价买入", "limit_sell": "限价卖出",
    "take_profit_stop_loss": "止盈止损", "pending_orders": "等待中",
}
REVIEW_KEY = "jd_desktop_review"
REVIEW_QUEUE_SECONDS = 25
DECIMAL = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def gram_text(value):
    try:
        grams = Decimal(str(value))
        if grams.is_finite() and 0 < grams < 1000000 and grams == grams.quantize(Decimal("0.0001")):
            return format(grams.normalize(), "f")
    except InvalidOperation:
        pass
    return None


def field_grams(labels):
    if len(labels) == 1 and DECIMAL.fullmatch(labels[0]):
        return Decimal(labels[0].replace(",", ""))
    return None


def is_jd_process(pid):
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        return bool(kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))) and (
            PureWindowsPath(path.value).name.casefold() == "京东金融.exe"
        )
    finally:
        kernel.CloseHandle(handle)


def product_matches(url, bank, path=PRODUCT_PATH):
    page = urlsplit(url)
    return (
        f"{page.scheme}://{page.netloc}" == PROVIDER
        and page.path == path
        and parse_qs(page.query).get("productSku") == [PRODUCTS[bank][0]]
    )


def value_after(labels, label, *, nonnegative=True):
    if labels.count(label) != 1:
        return None
    index = labels.index(label) + 1
    if index >= len(labels) or not DECIMAL.fullmatch(labels[index]):
        return None
    return number(labels[index].replace(",", ""), nonnegative)


def parse_cash_panel(labels, bank):
    if labels.count("付款账户") != 1 or sum(labels.count(marker) for marker in BUY_UNIT_MARKERS) != 1:
        return None
    index = labels.index("付款账户") + 1
    payment = labels[index:index + 6]
    if (len(payment) != 6 or payment[:2] != [PAYMENT_BANK[bank], "(余额"]
            or payment[3:] != [")", "买金专户，交易无限额", "立即买入"]):
        return None
    amount = payment[2].removeprefix("¥").removeprefix("￥").strip()
    return number(amount.replace(",", ""), True) if DECIMAL.fullmatch(amount) else None


def empty_waiting_orders(labels):
    return labels == [
        "限价买入", "限价卖出", "止盈止损", "等待中",
        "还没有限价订单哦", "查看交易单历史记录",
    ]


def fee_coupon_discount(labels):
    discounts = [label for label in labels if FEE_DISCOUNT.fullmatch(label)]
    if (len(discounts) == 1 and "手续费优惠" in labels and "暂不使用" not in labels
            and any(label.startswith("有效期至 ") for label in labels)):
        return discounts[0]
    return None


def parse_product(labels, bank, observed_at, cash=None):
    total = value_after(labels, "持仓克重")
    if total is None:
        raise ValueError("Desktop holdings are not visible")
    cost = value_after(labels, "成本均价(元/克)")
    pnl = value_after(labels, "持仓收益(元)", nonnegative=False)
    price = value_after(labels, DISPLAY_PRICE[bank] + "(元/克)")
    sellable = value_after(labels, "可卖克重")
    frozen = value_after(labels, "冻结克重")
    if ((sellable or 0) + (frozen or 0)) > total + 0.0001:
        raise ValueError("Inconsistent desktop holdings")
    return {
        "source": SOURCE,
        "product_sku": PRODUCTS[bank][0],
        "total_grams": total,
        "sellable_grams": sellable,
        "frozen_grams": frozen,
        "average_cost_cny": cost,
        "holding_pnl_cny": pnl,
        "cash_cny": cash,
        "cash_source": CASH_SOURCE if cash is not None else "unknown",
        "cash_scope": "bank_gold_electronic_account_not_linked_bank_card" if cash is not None else None,
        "display_price": {"price_cny": price, "observed_at": observed_at} if price else None,
        "coverage": {"account": "available", "cash_balance": "available" if cash is not None else "not_visible",
                     "pending_orders": "not_visible", "quote": "not_visible"},
        "component_times": {"account": observed_at, **({"cash_balance": observed_at} if cash is not None else {})},
    }


class DesktopReader:
    """Scope data reads and panel navigation to the identified product document."""

    def __init__(self):
        from comtypes.client import CreateObject, GetModule

        GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia

        self.uia = uia
        self.client = CreateObject(uia.CUIAutomation8, interface=uia.IUIAutomation)
        options = self.client.QueryInterface(uia.IUIAutomation2)
        options.ConnectionTimeout = 1500
        options.TransactionTimeout = 3000

    @staticmethod
    def elements(parent, scope, condition):
        result = parent.FindAll(scope, condition)
        return (result.GetElement(i) for i in range(result.Length))

    def windows(self):
        uia, client = self.uia, self.client
        name = client.CreatePropertyCondition(uia.UIA_NamePropertyId, WINDOW_NAME)
        for window in self.elements(client.GetRootElement(), uia.TreeScope_Children, name):
            if is_jd_process(window.CurrentProcessId):
                yield window

    def products(self, banks, path=PRODUCT_PATH):
        uia, client = self.uia, self.client
        document = client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_DocumentControlTypeId)
        for window in self.windows():
            for page in self.elements(window, uia.TreeScope_Descendants, document):
                url = page.GetCurrentPropertyValue(uia.UIA_ValueValuePropertyId)
                if not isinstance(url, str):
                    continue
                bank = next((bank for bank in banks if product_matches(url, bank, path)), None)
                if bank is not None and not page.CurrentIsOffscreen:
                    yield bank, page

    def labels(self, page):
        uia = self.uia
        static_text = self.client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_TextControlTypeId)
        return [item.CurrentName.strip().replace("（", "(").replace("）", ")")
                for item in self.elements(page, uia.TreeScope_Descendants, static_text)
                if not item.CurrentIsOffscreen and item.CurrentName]

    def dialogs(self, page):
        uia = self.uia
        window_type = self.client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_WindowControlTypeId)
        return [dialog for dialog in self.elements(page, uia.TreeScope_Descendants, window_type)
                if not dialog.CurrentIsOffscreen]

    def read(self, banks, observed_at):
        output = {}
        for bank, page in self.products(banks):
            try:
                labels = self.labels(page)
                dialogs = [self.labels(dialog) for dialog in self.dialogs(page)]
                cash = parse_cash_panel(dialogs[0], bank) if len(dialogs) == 1 else None
                for dialog in dialogs:
                    # Modal contents can repeat labels from the product page.
                    start = next((i for i in range(len(labels)) if labels[i:i+len(dialog)] == dialog), None)
                    if start is not None:
                        del labels[start:start+len(dialog)]
                output[bank] = parse_product(labels, bank, observed_at, cash)
            except ValueError:
                continue
        return output

    def product_page(self, bank):
        pages = list(self.products((bank,)))
        if len(pages) != 1:
            raise ValueError("Open the intended bank's gold product in JD Finance")
        return pages[0][1]

    def landing_page(self, bank):
        page = self.product_page(bank)
        if self.dialogs(page) or list(self.products((bank,), ORDERS_PATH)):
            raise ValueError("A dialog is already open")
        labels = self.labels(page)
        if labels.count("持仓克重") != 1 or labels.count(DISPLAY_PRICE[bank] + "(元/克)") != 1:
            raise ValueError("The gold product landing page is not visible")
        return page

    def invoke(self, item):
        uia = self.uia
        pattern = item.GetCurrentPattern(uia.UIA_InvokePatternId)
        pattern.QueryInterface(uia.IUIAutomationInvokePattern).Invoke()

    def open_buy_panel(self, bank):
        """Open the product landing page's Buy panel to view its payment options."""
        self.open_market_panel(bank, "market_buy")

    def open_market_panel(self, bank, action):
        label, class_name, _ = MARKET_PANELS[action]
        uia = self.uia
        page = self.landing_page(bank)
        edits = self.client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_EditControlTypeId)
        if any(not item.CurrentIsOffscreen for item in self.elements(page, uia.TreeScope_Descendants, edits)):
            raise ValueError("A form is already open")
        name = self.client.CreatePropertyCondition(uia.UIA_NamePropertyId, label)
        buttons = [
            item for item in self.elements(page, uia.TreeScope_Descendants, name)
            if item.CurrentControlType == uia.UIA_ButtonControlTypeId
            and item.CurrentIsEnabled and not item.CurrentIsOffscreen
            and class_name in item.CurrentClassName.split()
        ]
        if len(buttons) != 1:
            raise ValueError("Cannot identify one market panel opener")
        self.invoke(buttons[0])

    def market_dialog(self, bank, action):
        pages = list(self.products((bank,)))
        if len(pages) != 1:
            return None
        dialogs = self.dialogs(pages[0][1])
        if len(dialogs) != 1:
            return None
        labels = self.labels(dialogs[0])
        markers, _ = MARKET_CLOSE_CONTROLS[action]
        if MARKET_PANELS[action][2] in labels and sum(labels.count(marker) for marker in markers) == 1:
            return dialogs[0]
        return None

    def market_panel_visible(self, bank, action):
        return self.market_dialog(bank, action) is not None

    def buy_weight_choice(self, bank):
        dialog = self.market_dialog(bank, "market_buy")
        if dialog is None:
            return None, False
        uia = self.uia
        condition = self.client.CreatePropertyCondition(uia.UIA_NamePropertyId, "克重")
        walker = self.client.RawViewWalker
        matches = []
        for item in self.elements(dialog, uia.TreeScope_Descendants, condition):
            if item.CurrentControlType != uia.UIA_TextControlTypeId or item.CurrentIsOffscreen:
                continue
            tab = walker.GetParentElement(item)
            tab_group = walker.GetParentElement(tab)
            if (tab.CurrentIsEnabled and not tab.CurrentIsOffscreen
                    and tab.GetCurrentPropertyValue(uia.UIA_IsInvokePatternAvailablePropertyId)
                    and "cf-gold-sd-buy-index-money__title-select" in tab_group.CurrentClassName.split()
                    and self.labels(tab) == ["克重"] and self.labels(tab_group) == ["金额", "克重"]):
                matches.append(tab)
        if len(matches) != 1:
            return None, False
        tab = matches[0]
        selected = "buy-select" in tab.CurrentClassName.split() and "买入克重" in self.labels(dialog)
        return tab, selected

    def close_buy_panel(self, bank):
        self.close_market_panel(bank, "market_buy")

    def close_market_panel(self, bank, action):
        _, class_name = MARKET_CLOSE_CONTROLS[action]
        dialog = self.market_dialog(bank, action)
        if dialog is None:
            raise ValueError("The market panel cannot be identified")
        uia = self.uia
        condition = self.client.CreatePropertyCondition(uia.UIA_ClassNamePropertyId, class_name)
        matches = [item for item in self.elements(dialog, uia.TreeScope_Descendants, condition)
                   if item.CurrentIsEnabled and not item.CurrentIsOffscreen]
        if len(matches) != 1:
            raise ValueError("The market panel close control cannot be identified")
        self.invoke(matches[0])

    def open_orders_panel(self, bank):
        page = self.landing_page(bank)
        uia = self.uia
        condition = self.client.CreatePropertyCondition(uia.UIA_ClassNamePropertyId, "bottom-view-wrap__config-dl")
        matches = [item for item in self.elements(page, uia.TreeScope_Descendants, condition)
                   if item.CurrentIsEnabled and not item.CurrentIsOffscreen and self.labels(item) == ["条件单"]]
        if len(matches) != 1:
            raise ValueError("Cannot identify one conditional order viewer")
        self.invoke(matches[0])

    def open_waiting_orders(self, bank):
        self.open_order_tab(bank, "等待中")

    def order_tab(self, bank, label):
        pages = list(self.products((bank,), ORDERS_PATH))
        if len(pages) != 1:
            return None
        _, page = pages[0]
        uia = self.uia
        labels = self.labels(page)
        if labels[:len(ORDER_TABS)] != list(ORDER_TABS.values()) or self.dialogs(page):
            return None
        condition = self.client.CreatePropertyCondition(uia.UIA_NamePropertyId, label)
        links = [item for item in self.elements(page, uia.TreeScope_Descendants, condition)
                 if item.CurrentControlType == uia.UIA_TextControlTypeId and not item.CurrentIsOffscreen]
        if len(links) != 1:
            return None
        parent = self.client.RawViewWalker.GetParentElement(links[0])
        if (not parent.CurrentIsEnabled
                or parent.CurrentIsOffscreen
                or not parent.GetCurrentPropertyValue(uia.UIA_IsInvokePatternAvailablePropertyId)
                or self.labels(parent) != [label]):
            return None
        return parent

    def open_order_tab(self, bank, label):
        tab = wait_for(lambda: self.order_tab(bank, label), lambda item: item is not None)
        if tab is None:
            raise ValueError("Cannot identify one conditional order tab")
        if "active" not in tab.CurrentClassName.split():
            self.invoke(tab)

    def order_tab_active(self, bank, label):
        tab = self.order_tab(bank, label)
        return tab is not None and "active" in tab.CurrentClassName.split()

    def visible_controls(self, scope, class_name):
        uia = self.uia
        condition = self.client.CreatePropertyCondition(uia.UIA_IsInvokePatternAvailablePropertyId, True)
        return [item for item in self.elements(scope, uia.TreeScope_Descendants, condition)
                if item.CurrentIsEnabled and not item.CurrentIsOffscreen
                and class_name in item.CurrentClassName.split()]

    def sell_coupon_row(self, bank):
        pages = list(self.products((bank,)))
        if len(pages) != 1:
            return None
        dialogs = self.dialogs(pages[0][1])
        if len(dialogs) != 1 or "立即卖出" not in self.labels(dialogs[0]):
            return None
        rows = [row for row in self.visible_controls(dialogs[0], SELL_COUPON_ROW)
                if self.labels(row).count("优惠") == 1]
        return rows[0] if len(rows) == 1 else None

    def sell_coupon_picker(self, bank):
        pages = list(self.products((bank,)))
        if len(pages) != 1:
            return None
        dialogs = self.dialogs(pages[0][1])
        if len(dialogs) != 2 or not any({"立即卖出", "卖出克重(克)"} <= set(self.labels(dialog)) for dialog in dialogs):
            return None
        sheets = [sheet for sheet in dialogs
                  if f"{SELL_COUPON_POPUP}__sheet" in sheet.CurrentClassName.split()
                  and self.labels(sheet)[:1] == ["请选择优惠"]]
        return sheets[0] if len(sheets) == 1 else None

    def select_sell_coupon(self, bank):
        picker = self.sell_coupon_picker(bank)
        if picker is not None:
            return self.apply_sell_coupon(bank, picker)
        row = wait_for(lambda: self.sell_coupon_row(bank), lambda item: item is not None)
        if row is None:
            return "needs_review"
        labels = self.labels(row)
        counts = [int(label.removesuffix("项优惠")) for label in labels if re.fullmatch(r"\d+项优惠", label)]
        if counts == [0]:
            return "none"
        if "暂不使用" not in labels and any(label.startswith("手续费") and
                FEE_DISCOUNT.fullmatch(label.removeprefix("手续费")) for label in labels):
            return "applied"
        if "暂不使用" not in labels or len(counts) != 1:
            return "needs_review"

        self.invoke(row)
        picker = wait_for(lambda: self.sell_coupon_picker(bank), lambda item: item is not None)
        if picker is None or counts != [1]:
            return "needs_review"
        return self.apply_sell_coupon(bank, picker)

    def apply_sell_coupon(self, bank, picker):
        items = self.visible_controls(picker, f"{SELL_COUPON_POPUP}__item")
        unused = f"{SELL_COUPON_POPUP}__unused"
        selected = f"{SELL_COUPON_POPUP}__item--selected"
        candidates = [item for item in items if unused not in item.CurrentClassName.split()]
        unselected = [item for item in items if {unused, selected} <= set(item.CurrentClassName.split())]
        if len(unselected) != 1 or len(candidates) != 1:
            return "needs_review"
        discount = fee_coupon_discount(self.labels(candidates[0]))
        if discount is None:
            return "needs_review"
        self.invoke(candidates[0])
        row = wait_for(lambda: self.sell_coupon_row(bank),
                       lambda item: item is not None and f"手续费{discount}" in self.labels(item))
        return "applied" if row is not None else "needs_review"

    def open_review(self, bank, action):
        if action == "market_buy" and self.buy_weight_choice(bank)[1]:
            return "grams"
        if action == "market_sell" and (self.market_panel_visible(bank, action)
                                        or self.sell_coupon_picker(bank) is not None):
            return self.select_sell_coupon(bank)
        if action in MARKET_PANELS:
            self.open_market_panel(bank, action)
            ready = wait_for(lambda: self.market_panel_visible(bank, action), bool)
        else:
            label = ORDER_TABS[action]
            self.open_orders_panel(bank)
            self.open_order_tab(bank, label)
            ready = wait_for(lambda: self.order_tab_active(bank, label), bool)
        if not ready:
            raise ValueError("Requested review page could not be verified")
        if action == "market_buy":
            choice = wait_for(lambda: self.buy_weight_choice(bank), lambda value: value[0] is not None)
            if choice is None:
                raise ValueError("The buy weight tab could not be identified")
            tab, selected = choice
            if not selected:
                self.invoke(tab)
                if not wait_for(lambda: self.buy_weight_choice(bank)[1], bool):
                    raise ValueError("The buy weight mode could not be verified")
            return "grams"
        if action == "market_sell":
            return self.select_sell_coupon(bank)

    def gram_field(self, bank, action):
        dialog = self.market_dialog(bank, action)
        if dialog is None or (action == "market_buy" and "买入克重" not in self.labels(dialog)):
            raise ValueError("The verified gram form is unavailable")
        field_class, _ = GRAM_INPUTS[action]
        fields = self.visible_controls(dialog, f"{field_class}-con")
        if len(fields) != 1:
            raise ValueError("The gram input is unavailable")
        return dialog, fields[0]

    def numeric_key(self, bank, action, character):
        field_class, keyboard = GRAM_INPUTS[action]
        try:
            _, field = self.gram_field(bank, action)
        except ValueError:
            return None
        if f"{field_class}-con--focus" not in field.CurrentClassName.split():
            return None
        uia = self.uia
        walker = self.client.RawViewWalker
        matches = []
        for key in self.visible_controls(self.product_page(bank), f"{keyboard}-key"):
            if key.CurrentControlType != uia.UIA_ButtonControlTypeId or key.CurrentName != character:
                continue
            wrapper = walker.GetParentElement(key)
            container = walker.GetParentElement(wrapper)
            if f"{keyboard}-number-keyboard" in container.CurrentClassName.split():
                matches.append(key)
        return matches[0] if len(matches) == 1 else None

    def form_price_matches(self, bank, action, plan):
        dialog = self.market_dialog(bank, action)
        if dialog is None:
            return False
        price = value_after(self.labels(dialog), DISPLAY_PRICE[bank] + "(元/克)")
        if not price:
            return False
        condition, limit = plan["price_condition"], plan["price_cny"]
        if condition == "at_or_below":
            return price <= limit
        if condition == "at_or_above":
            return price >= limit
        return condition in ("reference", "none")

    def prepare_market_order(self, bank, plan):
        action = f'market_{plan["action"]}'
        grams = Decimal(plan["grams"])
        self.landing_page(bank)

        def reject(status):
            self.close_market_panel(bank, action)
            return {"status": status}

        try:
            coupon = self.open_review(bank, action)
            if self.sell_coupon_picker(bank) is not None:
                return {"status": "coupon_required"}
            dialog, field = self.gram_field(bank, action)
            labels = self.labels(dialog)
            initial = self.labels(field)
            if not self.form_price_matches(bank, action, plan):
                return reject("price_unmet")
            if action == "market_buy":
                rule = BUY_GRAM_RULE.fullmatch(initial[0]) if len(initial) == 1 else None
                if rule is None:
                    return reject("quantity_unavailable")
                minimum, step = map(Decimal, rule.groups())
                if grams < minimum or step <= 0 or (grams - minimum) % step:
                    return reject("quantity_unavailable")
                cash = parse_cash_panel(labels, bank)
                price = value_after(labels, DISPLAY_PRICE[bank] + "(元/克)")
                if cash is None or not price or grams * Decimal(str(price)) > Decimal(str(cash)):
                    return reject("cash_unavailable")
            else:
                whole = field_grams(initial)
                rules = [match for label in labels if (match := SELL_WHOLE_RULE.match(label))]
                if whole is None or grams > whole or (rules and whole <= Decimal(rules[0][1]) and grams != whole):
                    return reject("quantity_unavailable")
                if grams == whole:
                    return {"status": "prepared", "coupon": coupon}
                prefix, _ = GRAM_INPUTS[action]
                clear = self.visible_controls(dialog, f"{prefix}-clear")
                if len(clear) != 1:
                    return reject("quantity_unavailable")
                self.invoke(clear[0])
                dialog, field = self.gram_field(bank, action)
                if field_grams(self.labels(field)) is not None:
                    raise ValueError("The default quantity did not clear")

            self.invoke(field)
            for index, character in enumerate(plan["grams"]):
                key = wait_for(lambda: self.numeric_key(bank, action, character), lambda item: item is not None)
                if key is None:
                    raise ValueError("The numeric keypad could not be verified")
                self.invoke(key)
                expected = plan["grams"][:index + 1]
                if not wait_for(lambda: self.gram_input_labels(bank, action),
                                lambda current: current == [expected]):
                    raise ValueError("The number typed into the gram field could not be verified")
            if not self.form_price_matches(bank, action, plan):
                return reject("price_unmet")
            if action == "market_buy":
                budget = Decimal(str(cash))
                if not wait_for(lambda: self.buy_payable(bank), lambda total: total is not None and 0 < total <= budget):
                    return reject("cash_unavailable")
            return {"status": "prepared", **({"coupon": coupon} if action == "market_sell" else {})}
        except Exception:
            # Only this method opens the draft and writes its digits. Discard partial
            # input before leaving it next to the client's trade button.
            try:
                self.close_market_panel(bank, action)
            except Exception:
                pass
            raise

    def gram_input_labels(self, bank, action):
        try:
            _, field = self.gram_field(bank, action)
        except ValueError:
            return None
        return self.labels(field)

    def buy_payable(self, bank):
        dialog = self.market_dialog(bank, "market_buy")
        if dialog is None:
            return None
        labels = self.labels(dialog)
        if labels.count("预计支付") != 1:
            return None
        value = labels[labels.index("预计支付") + 1:][:1]
        if not value or not value[0].endswith("元"):
            return None
        return field_grams([value[0].removesuffix("元").strip()])

    def read_empty_orders(self, bank):
        pages = list(self.products((bank,), ORDERS_PATH))
        if len(pages) != 1:
            return False
        _, page = pages[0]
        uia = self.uia
        condition = self.client.CreatePropertyCondition(uia.UIA_NamePropertyId, "等待中")
        waiting = list(self.elements(page, uia.TreeScope_Descendants, condition))
        return (
            len(waiting) == 1
            and "active" in self.client.RawViewWalker.GetParentElement(waiting[0]).CurrentClassName.split()
            and empty_waiting_orders(self.labels(page))
        )

    def close_orders_panel(self, bank):
        uia = self.uia
        waiting = list(self.products((bank,), ORDERS_PATH))
        if len(waiting) != 1:
            raise ValueError("Cannot identify the page being closed")
        name = self.client.CreatePropertyCondition(uia.UIA_NamePropertyId, "返回")
        matches = [item for window in self.windows()
                   for item in self.elements(window, uia.TreeScope_Descendants, name)
                   if item.CurrentControlType == uia.UIA_ButtonControlTypeId
                   and item.CurrentIsEnabled and not item.CurrentIsOffscreen]
        if len(matches) != 1:
            raise ValueError("Cannot identify one back control")
        self.invoke(matches[0])

    def refresh_panels(self, bank):
        """Fetch supplemental data and return to the landing page when possible."""
        values = {}
        now = lambda: datetime.now(timezone.utc).isoformat()
        try:
            self.open_buy_panel(bank)
            try:
                snapshot = wait_for(lambda: self.read((bank,), now()).get(bank, {}),
                                    lambda result: result.get("cash_cny") is not None)
                if snapshot:
                    values["cash"] = (snapshot["cash_cny"], snapshot["component_times"]["cash_balance"])
            finally:
                wait_for(lambda: self.dialogs(self.product_page(bank)), bool)
                self.close_buy_panel(bank)
        except ValueError:
            pass
        try:
            self.open_orders_panel(bank)
            try:
                if wait_for(lambda: list(self.products((bank,), ORDERS_PATH)), bool):
                    self.open_waiting_orders(bank)
                    if wait_for(lambda: self.read_empty_orders(bank), bool):
                        values["orders"] = now()
            finally:
                self.close_orders_panel(bank)
        except ValueError:
            pass
        return values


def wait_for(read, ready, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = read()
            if ready(result):
                return result
        except UI_READ_ERRORS:
            # The app can replace accessibility nodes while rerendering a form.
            pass
        time.sleep(0.25)
    return None


def review_state(conn):
    row = conn.execute("SELECT value FROM state WHERE key=?", (REVIEW_KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def write_review(conn, value, now):
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (REVIEW_KEY, json.dumps(value), now.isoformat()))


def request_review(conn, bank, action):
    if not isinstance(action, str) or action not in MARKET_PANELS | ORDER_TABS:
        raise ValueError("请选择有效的京东核对入口")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc)
        if bank not in enabled_banks(conn):
            raise ValueError("此银行账户未启用")
        old = review_state(conn)
        if ((old.get("status") == "queued" and age(old.get("requested_at"), now) < REVIEW_QUEUE_SECONDS)
                or (old.get("status") == "opening" and age(old.get("started_at"), now) < 60)):
            raise RuntimeError("京东正在打开另一个页面")
        value = {"id": uuid4().hex, "bank": bank, "action": action,
                 "status": "queued", "requested_at": now.isoformat()}
        write_review(conn, value, now)
    return value


def process_review(conn, reader):
    if review_state(conn).get("status") != "queued":
        return False
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        value = review_state(conn)
        if value.get("status") != "queued":
            return False
        now = datetime.now(timezone.utc)
        if age(value["requested_at"], now) >= REVIEW_QUEUE_SECONDS or value["bank"] not in enabled_banks(conn):
            write_review(conn, {**value, "status": "expired"}, now)
            return False
        value.update(status="opening", started_at=now.isoformat())
        write_review(conn, value, now)
    try:
        detail = reader.open_review(value["bank"], value["action"])
    except Exception:
        value["status"] = "unverified"
    else:
        value["status"] = "opened"
        if value["action"] == "market_buy" and detail == "grams":
            value["unit"] = detail
        elif value["action"] == "market_sell" and detail in ("applied", "none", "needs_review"):
            value["coupon"] = detail
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if review_state(conn).get("id") == value["id"]:
            write_review(conn, value, datetime.now(timezone.utc))
    return True


def sync_desktop(store, reader, *, refresh_panels=False):
    banks = enabled_banks(store.conn)
    if not banks:
        return "completed"
    observed_at = datetime.now(timezone.utc).isoformat()
    snapshots = reader.read(banks, observed_at)
    for bank in enabled_banks(store.conn):
        if bank not in snapshots:
            record_failure(store.conn, bank, "unavailable", observed_at)
            continue
        data = snapshots[bank]
        if refresh_panels:
            extra = reader.refresh_panels(bank)
            if "cash" in extra:
                data["cash_cny"], data["component_times"]["cash_balance"] = extra["cash"]
                data["cash_source"] = CASH_SOURCE
                data["cash_scope"] = "bank_gold_electronic_account_not_linked_bank_card"
                data["coverage"]["cash_balance"] = "available"
            if "orders" in extra:
                data["pending_order_count"] = 0
                data["pending_orders"] = {
                    "records": [], "total_count": 0, "complete": True,
                    "scope": "bank_pending_conditional_orders",
                }
                data["component_times"]["pending_orders"] = extra["orders"]
                data["coverage"]["pending_orders"] = "available"
        else:
            # A passive poll cannot renew supplemental data's observation time.
            row = store.conn.execute("SELECT data_json FROM jd_live_snapshots WHERE bank=?", (bank,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            if previous.get("source") == SOURCE and all(
                previous.get(key) == data.get(key) for key in ("total_grams", "average_cost_cny")
            ):
                preserve_components(data, previous)
        record_success(store.conn, bank, data, observed_at)
    if not snapshots:
        return "unavailable"
    return "completed"


def preserve_components(data, previous):
    components = {
        "cash_balance": ("cash_cny", "cash_source", "cash_scope"),
        "pending_orders": ("pending_orders", "pending_order_count"),
    }
    for operation, keys in components.items():
        if (data["coverage"].get(operation) == "available"
                or previous.get("coverage", {}).get(operation) != "available"):
            continue
        data.update({key: previous[key] for key in keys})
        data["component_times"][operation] = previous["component_times"][operation]
        data["coverage"][operation] = "available"
