import csv
import io
import math
import re
from decimal import Decimal, InvalidOperation
from email.parser import BytesParser
from email.policy import default as email_policy
from html import escape
from typing import Mapping, TypedDict
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="VyaparAI")

DEFAULT_BUDGET = "1000"
REQUIRED_CSV_COLUMNS = (
    "product",
    "packs_in_stock",
    "packs_sold_per_day",
    "supplier_delivery_days",
    "wholesale_cost_per_pack",
)
CSV_COLUMN_FOR_FIELD = {
    "stock": "packs_in_stock",
    "sales": "packs_sold_per_day",
    "delivery": "supplier_delivery_days",
    "cost": "wholesale_cost_per_pack",
}


class Product(TypedDict):
    slug: str
    name: str
    packs_in_stock: int
    packs_sold_per_day: Decimal
    supplier_delivery_days: int
    wholesale_cost_per_pack: Decimal


class ProductRowInput(TypedDict):
    slug: str
    name: str
    raw_stock: str
    raw_sales: str
    raw_delivery: str
    raw_cost: str
    errors: dict[str, str]
    product: Product | None


class OrderLine(TypedDict):
    name: str
    is_urgent: bool
    gap_days: float
    target_packs: int
    packs_to_buy: int
    cost: Decimal
    reason: str


class OrderPlan(TypedDict):
    budget: Decimal
    lines: list[OrderLine]
    total_cost: Decimal
    remaining_budget: Decimal


class SupplierOptionResult(TypedDict):
    key: str
    title: str
    raw_cost: str
    raw_delivery: str
    cost_error: str | None
    delivery_error: str | None
    packs_to_buy: int | None
    total_cost: Decimal | None
    money_left: Decimal | None
    gap_text: str | None
    gap_is_risk: bool


DEFAULT_OIL_SUPPLIERS = (
    ("regular", "Regular supplier", "150", "4"),
    ("faster", "Faster supplier", "170", "1"),
)


SAMPLE_PRODUCTS: list[Product] = [
    {
        "slug": "atta",
        "name": "Atta",
        "packs_in_stock": 10,
        "packs_sold_per_day": Decimal("5"),
        "supplier_delivery_days": 3,
        "wholesale_cost_per_pack": Decimal("100"),
    },
    {
        "slug": "rice",
        "name": "Rice",
        "packs_in_stock": 20,
        "packs_sold_per_day": Decimal("5"),
        "supplier_delivery_days": 2,
        "wholesale_cost_per_pack": Decimal("120"),
    },
    {
        "slug": "oil",
        "name": "Oil",
        "packs_in_stock": 4,
        "packs_sold_per_day": Decimal("2"),
        "supplier_delivery_days": 4,
        "wholesale_cost_per_pack": Decimal("150"),
    },
]


def format_decimal(value: Decimal) -> str:
    """Format a Decimal without unnecessary trailing zeros."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return format(normalized, "f")


def format_days(days: float) -> str:
    """Format a day count without trailing .0 when it is a whole number."""
    if days.is_integer():
        return str(int(days))
    return f"{days:.1f}"


def pluralize_days(days: float) -> str:
    """Return formatted day count with singular/plural 'day(s)' label."""
    unit = "day" if days == 1 else "days"
    return f"{format_days(days)} {unit}"


def format_rupees(amount: int | Decimal) -> str:
    """Format a rupee amount with the ₹ symbol and thousands separators."""
    if isinstance(amount, Decimal):
        normalized = amount.normalize()
        if normalized == normalized.to_integral_value():
            return f"₹{int(normalized):,}"
        return f"₹{amount.quantize(Decimal('0.01')):,.2f}"
    return f"₹{amount:,}"


def parse_non_negative_int(raw: str, label: str) -> tuple[int | None, str | None]:
    """Parse a non-negative whole number from form input."""
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return None, f"Enter {label} (0 or more)."
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None, f"Enter a whole number for {label} (0 or more)."
    if not dec.is_finite() or dec != dec.to_integral_value():
        return None, f"Enter a whole number for {label} (0 or more)."
    value = int(dec)
    if value < 0:
        return None, f"{label.capitalize()} cannot be negative."
    return value, None


def parse_non_negative_decimal(raw: str, label: str) -> tuple[Decimal | None, str | None]:
    """Parse a non-negative finite number from form input."""
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return None, f"Enter {label} (0 or more)."
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None, f"Enter a valid number for {label} (0 or more)."
    if not value.is_finite():
        return None, f"Enter a valid number for {label} (0 or more)."
    if value < 0:
        return None, f"{label.capitalize()} cannot be negative."
    return value, None


def parse_positive_cost(raw: str) -> tuple[Decimal | None, str | None]:
    """Parse a wholesale cost per pack strictly greater than zero."""
    cleaned = raw.strip()
    if cleaned.startswith("₹"):
        cleaned = cleaned[1:].strip()
    cleaned = cleaned.replace(",", "")
    if not cleaned:
        return None, "Enter a cost above ₹0."
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None, "Enter a valid price above ₹0."
    if not value.is_finite():
        return None, "Enter a valid price above ₹0."
    if value <= 0:
        return None, "Wholesale cost must be above ₹0."
    return value, None


def parse_budget(raw_budget: str) -> tuple[Decimal | None, str | None]:
    """Validate and parse the user's rupee budget string."""
    cleaned = raw_budget.strip()
    if cleaned.startswith("₹"):
        cleaned = cleaned[1:].strip()
    cleaned = cleaned.replace(",", "")

    if not cleaned:
        return None, "Please enter a budget in rupees (for example, 1000 or 0)."

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None, "Invalid budget. Please enter a valid non-negative number in rupees (for example, 1000 or 0)."

    if not amount.is_finite():
        return None, "Invalid budget. Please enter a finite number in rupees (for example, 1000 or 0)."

    if amount < 0:
        return None, "Budget cannot be negative. Please enter ₹0 or a positive amount."

    return amount, None


def validate_product_row(
    slug: str,
    name: str,
    raw_stock: str,
    raw_sales: str,
    raw_delivery: str,
    raw_cost: str,
) -> ProductRowInput:
    """Validate raw string fields for a single product row and build ProductRowInput."""
    errors: dict[str, str] = {}
    stock, err_stock = parse_non_negative_int(raw_stock, "stock")
    if err_stock:
        errors["stock"] = err_stock

    sales, err_sales = parse_non_negative_decimal(raw_sales, "daily sales")
    if err_sales:
        errors["sales"] = err_sales

    delivery, err_delivery = parse_non_negative_int(raw_delivery, "delivery days")
    if err_delivery:
        errors["delivery"] = err_delivery

    cost, err_cost = parse_positive_cost(raw_cost)
    if err_cost:
        errors["cost"] = err_cost

    product: Product | None = None
    if not errors and stock is not None and sales is not None and delivery is not None and cost is not None:
        product = {
            "slug": slug,
            "name": name,
            "packs_in_stock": stock,
            "packs_sold_per_day": sales,
            "supplier_delivery_days": delivery,
            "wholesale_cost_per_pack": cost,
        }

    return {
        "slug": slug,
        "name": name,
        "raw_stock": raw_stock,
        "raw_sales": raw_sales,
        "raw_delivery": raw_delivery,
        "raw_cost": raw_cost,
        "errors": errors,
        "product": product,
    }


def parse_product_rows(params: Mapping[str, str]) -> list[ProductRowInput]:
    """Extract and validate editable product rows from form or query parameters."""
    if "row_count" in params:
        try:
            row_count = int(params["row_count"])
        except ValueError:
            row_count = 0
        if 1 <= row_count <= 500:
            rows: list[ProductRowInput] = []
            for idx in range(row_count):
                fallback = SAMPLE_PRODUCTS[idx] if idx < len(SAMPLE_PRODUCTS) else None
                default_name = fallback["name"] if fallback else f"Item {idx + 1}"
                name = params.get(f"row_{idx}_name", default_name).strip() or default_name
                raw_stock = params.get(f"row_{idx}_stock", "")
                raw_sales = params.get(f"row_{idx}_sales", "")
                raw_delivery = params.get(f"row_{idx}_delivery", "")
                raw_cost = params.get(f"row_{idx}_cost", "")
                rows.append(
                    validate_product_row(
                        slug=f"row_{idx}",
                        name=name,
                        raw_stock=raw_stock,
                        raw_sales=raw_sales,
                        raw_delivery=raw_delivery,
                        raw_cost=raw_cost,
                    )
                )
            return rows

    parsed_rows: list[ProductRowInput] = []
    for sample in SAMPLE_PRODUCTS:
        slug = sample["slug"]
        raw_stock = params.get(f"{slug}_stock", str(sample["packs_in_stock"]))
        raw_sales = params.get(f"{slug}_sales", format_decimal(sample["packs_sold_per_day"]))
        raw_delivery = params.get(f"{slug}_delivery", str(sample["supplier_delivery_days"]))
        raw_cost = params.get(f"{slug}_cost", format_decimal(sample["wholesale_cost_per_pack"]))
        parsed_rows.append(
            validate_product_row(
                slug=slug,
                name=sample["name"],
                raw_stock=raw_stock,
                raw_sales=raw_sales,
                raw_delivery=raw_delivery,
                raw_cost=raw_cost,
            )
        )
    return parsed_rows


def generate_sample_csv() -> str:
    """Return the built-in sample inventory as a CSV string."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(REQUIRED_CSV_COLUMNS)
    for sample in SAMPLE_PRODUCTS:
        writer.writerow(
            [
                sample["name"],
                sample["packs_in_stock"],
                format_decimal(sample["packs_sold_per_day"]),
                sample["supplier_delivery_days"],
                format_decimal(sample["wholesale_cost_per_pack"]),
            ]
        )
    return buf.getvalue()


def parse_inventory_csv(csv_bytes: bytes) -> tuple[list[ProductRowInput] | None, list[str]]:
    """Parse and validate an uploaded inventory CSV into ProductRowInput rows."""
    if not csv_bytes.strip():
        return None, ["The uploaded CSV file is empty. Please upload a CSV with product rows."]

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ["Could not read the uploaded file as UTF-8 CSV text."]

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        raw_header = next(reader)
    except StopIteration:
        return None, [
            "The uploaded CSV file is empty. Expected columns: "
            + ", ".join(REQUIRED_CSV_COLUMNS)
            + "."
        ]

    header_map: dict[str, int] = {}
    duplicate_cols: list[str] = []
    for idx, col_name in enumerate(raw_header):
        normalized = col_name.strip().lower()
        if not normalized:
            continue
        if normalized in header_map:
            duplicate_cols.append(normalized)
        else:
            header_map[normalized] = idx

    missing_cols = [col for col in REQUIRED_CSV_COLUMNS if col not in header_map]
    if missing_cols:
        return None, [
            f"Missing required column(s): {', '.join(missing_cols)}. "
            f"Expected columns: {', '.join(REQUIRED_CSV_COLUMNS)}."
        ]

    if duplicate_cols:
        return None, [f"Duplicate column header(s): {', '.join(duplicate_cols)}."]

    parsed_rows: list[ProductRowInput] = []
    errors: list[str] = []
    data_row_count = 0

    for row_num, raw_row in enumerate(reader, start=2):
        if all(not cell.strip() for cell in raw_row):
            continue
        data_row_count += 1

        if len(raw_row) != len(raw_header):
            errors.append(
                f"Row {row_num}: expected {len(raw_header)} columns, found {len(raw_row)}."
            )
            continue

        raw_name = raw_row[header_map["product"]].strip()
        raw_stock = raw_row[header_map["packs_in_stock"]].strip()
        raw_sales = raw_row[header_map["packs_sold_per_day"]].strip()
        raw_delivery = raw_row[header_map["supplier_delivery_days"]].strip()
        raw_cost = raw_row[header_map["wholesale_cost_per_pack"]].strip()

        row_label = raw_name or "unnamed product"
        if not raw_name:
            errors.append(f"Row {row_num}, column 'product': product name cannot be empty.")

        row_input = validate_product_row(
            slug=f"row_{len(parsed_rows)}",
            name=raw_name,
            raw_stock=raw_stock,
            raw_sales=raw_sales,
            raw_delivery=raw_delivery,
            raw_cost=raw_cost,
        )
        for field_key, err_msg in row_input["errors"].items():
            col_name = CSV_COLUMN_FOR_FIELD[field_key]
            errors.append(f"Row {row_num} ({row_label}), column '{col_name}': {err_msg}")

        if raw_name and not row_input["errors"]:
            parsed_rows.append(row_input)

    if data_row_count == 0:
        return None, ["The uploaded CSV has headers only and no product rows."]

    if errors:
        return None, errors

    return parsed_rows, []


def parse_multipart_form(
    content_type: str, body: bytes
) -> tuple[dict[str, str], tuple[str, bytes] | None]:
    """Parse multipart/form-data request body without requiring python-multipart."""
    if not content_type.lower().startswith("multipart/form-data"):
        return {}, None

    raw_message = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + body
    msg = BytesParser(policy=email_policy).parsebytes(raw_message)
    if not msg.is_multipart():
        return {}, None

    fields: dict[str, str] = {}
    uploaded_file: tuple[str, bytes] | None = None

    for part in msg.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""

        if filename is not None or field_name in ("file", "csv_file"):
            if uploaded_file is None or filename:
                uploaded_file = (filename or "", payload)
        elif field_name:
            fields[field_name] = payload.decode("utf-8", errors="replace")

    return fields, uploaded_file


def calculate_stock_metrics(product: Product) -> tuple[float | None, float | None]:
    """Return (days_of_stock, stockout_gap_days), or (None, None) when daily sales are zero."""
    stock = product["packs_in_stock"]
    daily_sales = product["packs_sold_per_day"]
    delivery_days = product["supplier_delivery_days"]

    if daily_sales <= 0:
        return None, None

    days_of_stock = float(Decimal(stock) / daily_sales)
    gap_days = delivery_days - days_of_stock
    return days_of_stock, gap_days


def evaluate_restock(product: Product) -> tuple[float | None, str, str]:
    """Calculate days of stock and return (days_of_stock, status_label, detail_text)."""
    days_of_stock, gap_days = calculate_stock_metrics(product)
    if days_of_stock is None or gap_days is None:
        return None, "No recent sales", "Daily sales are 0, so stock duration cannot be estimated."

    delivery_days = product["supplier_delivery_days"]
    if gap_days > 0:
        explanation = (
            f"Stock lasts {pluralize_days(days_of_stock)}, while delivery takes "
            f"{pluralize_days(delivery_days)}—a gap of {pluralize_days(gap_days)} without stock."
        )
        return days_of_stock, "Restock soon", explanation

    return days_of_stock, "Enough stock until delivery.", ""


def plan_order(products: list[Product], budget: Decimal) -> OrderPlan:
    """Recommend pack quantities for urgent products within the given rupee budget."""
    urgent: list[tuple[float, Product]] = []
    non_urgent: list[tuple[float, Product]] = []
    no_sales: list[Product] = []

    for product in products:
        days_of_stock, gap_days = calculate_stock_metrics(product)
        if days_of_stock is None or gap_days is None:
            no_sales.append(product)
        elif gap_days > 0:
            urgent.append((gap_days, product))
        else:
            non_urgent.append((days_of_stock, product))

    # Largest projected stockout gap first; break ties alphabetically by product name.
    urgent.sort(key=lambda item: (-item[0], item[1]["name"].lower()))

    remaining = budget
    total_cost = Decimal("0")
    lines: list[OrderLine] = []

    for priority, (gap_days, product) in enumerate(urgent, start=1):
        unit_cost = product["wholesale_cost_per_pack"]
        target_packs = math.ceil(Decimal("2") * product["packs_sold_per_day"])
        affordable_packs = int(remaining // unit_cost)
        packs_to_buy = min(target_packs, max(0, affordable_packs))
        line_cost = Decimal(packs_to_buy) * unit_cost

        gap_label = pluralize_days(gap_days)
        if packs_to_buy == target_packs:
            reason = (
                f"Priority #{priority} ({gap_label} gap). Buying full 2-day sales target "
                f"of {target_packs} packs at {format_rupees(unit_cost)}/pack."
            )
        elif packs_to_buy > 0:
            reason = (
                f"Priority #{priority} ({gap_label} gap). Aimed for {target_packs} packs "
                f"(2 days of sales), but remaining budget of {format_rupees(remaining)} "
                f"only covered {packs_to_buy} packs at {format_rupees(unit_cost)}/pack."
            )
        elif remaining <= 0:
            reason = (
                f"Priority #{priority} ({gap_label} gap). Aimed for {target_packs} packs "
                f"(2 days of sales), but no budget remains—skipped."
            )
        else:
            reason = (
                f"Priority #{priority} ({gap_label} gap). Aimed for {target_packs} packs "
                f"(2 days of sales), but remaining budget of {format_rupees(remaining)} is "
                f"less than {format_rupees(unit_cost)} for 1 pack—skipped."
            )

        remaining -= line_cost
        total_cost += line_cost
        lines.append(
            {
                "name": product["name"],
                "is_urgent": True,
                "gap_days": gap_days,
                "target_packs": target_packs,
                "packs_to_buy": packs_to_buy,
                "cost": line_cost,
                "reason": reason,
            }
        )

    for days_of_stock, product in non_urgent:
        delivery_days = product["supplier_delivery_days"]
        lines.append(
            {
                "name": product["name"],
                "is_urgent": False,
                "gap_days": delivery_days - days_of_stock,
                "target_packs": 0,
                "packs_to_buy": 0,
                "cost": Decimal("0"),
                "reason": (
                    f"Can wait—{pluralize_days(days_of_stock)} of stock covers the "
                    f"{pluralize_days(delivery_days)} supplier delivery."
                ),
            }
        )

    for product in no_sales:
        lines.append(
            {
                "name": product["name"],
                "is_urgent": False,
                "gap_days": 0.0,
                "target_packs": 0,
                "packs_to_buy": 0,
                "cost": Decimal("0"),
                "reason": "Skipped—no recent sales (0 packs sold per day).",
            }
        )

    return {
        "budget": budget,
        "lines": lines,
        "total_cost": total_cost,
        "remaining_budget": remaining,
    }


def render_cell_input(
    field_id: str,
    value: str,
    aria_label: str,
    error: str | None,
    prefix: str = "",
) -> str:
    """Render an editable table input cell with an optional inline error beside it."""
    invalid_cls = " input-invalid" if error else ""
    error_html = f'<div class="field-error" role="alert">{escape(error)}</div>' if error else ""
    if prefix:
        control = (
            f'<div class="cell-prefix-wrap{invalid_cls}">'
            f'<span class="cell-prefix" aria-hidden="true">{escape(prefix)}</span>'
            f'<input class="table-input" id="{field_id}" name="{field_id}" type="text" '
            f'inputmode="decimal" aria-label="{escape(aria_label)}" value="{escape(value)}" />'
            f"</div>"
        )
    else:
        control = (
            f'<input class="table-input standalone-input{invalid_cls}" id="{field_id}" '
            f'name="{field_id}" type="text" inputmode="decimal" '
            f'aria-label="{escape(aria_label)}" value="{escape(value)}" />'
        )
    return f"{control}{error_html}"


def build_table_rows(row_inputs: list[ProductRowInput]) -> str:
    """Render editable HTML table rows for the inventory table."""
    rows: list[str] = []
    for idx, row in enumerate(row_inputs):
        field_prefix = f"row_{idx}"
        name = row["name"]
        errors = row["errors"]
        product = row["product"]

        stock_cell = render_cell_input(
            f"{field_prefix}_stock",
            row["raw_stock"],
            f"{name} packs in stock",
            errors.get("stock"),
        )
        sales_cell = render_cell_input(
            f"{field_prefix}_sales",
            row["raw_sales"],
            f"{name} packs sold per day",
            errors.get("sales"),
        )
        delivery_cell = render_cell_input(
            f"{field_prefix}_delivery",
            row["raw_delivery"],
            f"{name} supplier delivery days",
            errors.get("delivery"),
        )
        cost_cell = render_cell_input(
            f"{field_prefix}_cost",
            row["raw_cost"],
            f"{name} wholesale cost per pack",
            errors.get("cost"),
            prefix="₹",
        )

        if product is None:
            days_cell = "—"
            status_cell = '<span class="badge badge-error">Invalid input</span>'
        else:
            days_of_stock, status, detail = evaluate_restock(product)
            days_cell = "—" if days_of_stock is None else pluralize_days(days_of_stock)
            if status == "Restock soon":
                badge_class = "badge-warn"
            elif status == "No recent sales":
                badge_class = "badge-neutral"
            else:
                badge_class = "badge-ok"
            detail_html = f'<div class="detail">{escape(detail)}</div>' if detail else ""
            status_cell = f'<span class="badge {badge_class}">{escape(status)}</span>{detail_html}'

        rows.append(
            f"""            <tr>
                <td>
                    <strong>{escape(name)}</strong>
                    <input type="hidden" name="{field_prefix}_name" value="{escape(name)}" />
                </td>
                <td>{stock_cell}</td>
                <td>{sales_cell}</td>
                <td>{delivery_cell}</td>
                <td>{cost_cell}</td>
                <td class="nowrap">{days_cell}</td>
                <td>{status_cell}</td>
            </tr>"""
        )
    return "\n".join(rows)


def build_order_results_html(
    has_product_errors: bool,
    budget_error: str | None,
    plan: OrderPlan | None,
) -> str:
    """Render the order summary cards and recommended order table."""
    if has_product_errors and budget_error:
        return '<div class="alert-error" role="alert">Fix the invalid product values and budget above to generate an order recommendation.</div>'
    if has_product_errors:
        return '<div class="alert-error" role="alert">Fix the highlighted product values above to generate an order recommendation.</div>'
    if plan is None:
        return ""

    order_rows: list[str] = []
    for line in plan["lines"]:
        pack_word = "pack" if line["packs_to_buy"] == 1 else "packs"
        badge_class = "badge-buy" if line["packs_to_buy"] > 0 else "badge-skip"
        qty_label = f'{line["packs_to_buy"]} {pack_word}'
        order_rows.append(
            f"""                    <tr>
                        <td><strong>{escape(line["name"])}</strong></td>
                        <td><span class="badge {badge_class}">{qty_label}</span></td>
                        <td>{format_rupees(line["cost"])}</td>
                        <td class="reason-cell">{escape(line["reason"])}</td>
                    </tr>"""
        )
    rows_markup = "\n".join(order_rows)
    return f"""            <div class="summary-grid" aria-label="Order budget summary">
                <div class="summary-card">
                    <span class="summary-label">Budget</span>
                    <span class="summary-value">{format_rupees(plan["budget"])}</span>
                </div>
                <div class="summary-card">
                    <span class="summary-label">Total Order Cost</span>
                    <span class="summary-value">{format_rupees(plan["total_cost"])}</span>
                </div>
                <div class="summary-card">
                    <span class="summary-label">Remaining Budget</span>
                    <span class="summary-value">{format_rupees(plan["remaining_budget"])}</span>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Product</th>
                            <th>Quantity to buy</th>
                            <th>Cost</th>
                            <th>Reason</th>
                        </tr>
                    </thead>
                    <tbody>
{rows_markup}
                    </tbody>
                </table>
            </div>"""


def format_supplier_message(plan: OrderPlan | None) -> str:
    """Build a supplier restock message from products with quantity above zero."""
    if plan is None:
        return ""

    phrases: list[str] = []
    for line in plan["lines"]:
        qty = line["packs_to_buy"]
        if qty <= 0:
            continue
        pack_word = "pack" if qty == 1 else "packs"
        phrases.append(f"{qty} {pack_word} of {line['name']}")

    if not phrases:
        return ""

    if len(phrases) == 1:
        items_text = phrases[0]
    elif len(phrases) == 2:
        items_text = f"{phrases[0]} and {phrases[1]}"
    else:
        items_text = f"{', '.join(phrases[:-1])}, and {phrases[-1]}"

    return f"Namaste, please send {items_text}. Please confirm availability and delivery date."


def build_supplier_draft_html(plan: OrderPlan | None) -> str:
    """Render the Draft supplier message section below the recommended order."""
    draft_message = format_supplier_message(plan)

    if not draft_message:
        body_html = '<p class="draft-empty">No items to order.</p>'
    else:
        body_html = render_draft_editor_html(
            draft_message=draft_message,
            textarea_id="supplier-message",
            copy_btn_id="copy-message-btn",
            whatsapp_btn_id="open-whatsapp-btn",
            status_id="copy-status",
        )

    return f"""        <section class="draft-section" aria-label="Draft supplier message">
            <div class="planner-header">
                <h2>Draft supplier message</h2>
                <p class="section-note">Review or edit the order message before copying or opening WhatsApp to choose your recipient.</p>
            </div>
            {body_html}
        </section>"""


def build_csv_feedback_html(
    csv_errors: list[str] | None,
    csv_notice: str | None,
) -> str:
    """Render upload validation errors or confirmation banner."""
    if csv_errors:
        items_html = "".join(f"<li>{escape(err)}</li>" for err in csv_errors)
        return (
            '<div class="alert-error csv-alert" role="alert">'
            "<strong>CSV upload could not be applied (current inventory kept):</strong>"
            f'<ul class="error-list">{items_html}</ul>'
            "</div>"
        )
    if csv_notice:
        return f'<div class="alert-success csv-alert" role="status">{escape(csv_notice)}</div>'
    return ""


CURRENCY_PREFIX_RE = re.compile(r"(?:₹|rs\.?|inr)\s*(-?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
CURRENCY_SUFFIX_RE = re.compile(
    r"(?<![\w-])(-?\d[\d,]*(?:\.\d+)?)\s*(?:₹|rs\.?|inr|rupees?|rupaye|rupaiya)\b",
    re.IGNORECASE,
)
STANDALONE_NUM_RE = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(?![\w.])")
RANGE_OR_CHOICE_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:ya|or|to|-|–|/)\s*(?:₹|rs\.?|inr)?\s*\d",
    re.IGNORECASE,
)


def extract_budget_from_question(question: str) -> tuple[Decimal | None, str | None]:
    """Extract a single non-negative rupee budget from a natural-language question."""
    cleaned = question.strip()
    if not cleaned:
        return None, (
            "Budget missing hai—kripya ek specific amount likhein, jaise "
            "\"Mere paas ₹600 hain, kya mangwau?\""
        )

    if RANGE_OR_CHOICE_RE.search(cleaned):
        return None, (
            "Budget clear nahi hai (ek se zyada options dikh rahe hain)—kripya sirf ek "
            "pakka amount likhein, jaise \"Mere paas ₹600 hain, kya mangwau?\""
        )

    currency_matches = CURRENCY_PREFIX_RE.findall(cleaned) + CURRENCY_SUFFIX_RE.findall(cleaned)
    raw_candidates = currency_matches if currency_matches else STANDALONE_NUM_RE.findall(cleaned)

    parsed_amounts: list[Decimal] = []
    for token in raw_candidates:
        try:
            val = Decimal(token.replace(",", ""))
        except InvalidOperation:
            continue
        if val.is_finite() and val not in parsed_amounts:
            parsed_amounts.append(val)

    if not parsed_amounts:
        return None, (
            "Budget samajh nahi aaya—kripya ek specific amount batayein, jaise "
            "\"Mere paas ₹600 hain, kya mangwau?\""
        )

    if len(parsed_amounts) > 1:
        return None, (
            "Ek se zyada numbers dikhe hain, isliye budget clear nahi hai—kripya ek "
            "specific rupee amount likhein (jaise ₹600)."
        )

    amount = parsed_amounts[0]
    if amount < 0:
        return None, (
            "Budget negative nahi ho sakta—kripya ₹0 ya usse zyada ka specific amount batayein."
        )

    return amount, None


def format_hinglish_list(items: list[str]) -> str:
    """Join Hinglish item names naturally using 'aur'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} aur {items[1]}"
    return f"{', '.join(items[:-1])} aur {items[-1]}"


def build_hinglish_explanation(
    plan: OrderPlan | None,
    has_product_errors: bool,
    budget: Decimal,
) -> str:
    """Build a short Hinglish explanation grounded in the calculated OrderPlan."""
    budget_str = format_rupees(budget)
    if has_product_errors or plan is None:
        return (
            f"Aapke {budget_str} budget ke hisaab se order banane se pehle upar inventory table "
            "ki galat values theek karein."
        )

    bought = [line for line in plan["lines"] if line["packs_to_buy"] > 0]
    skipped_budget = [line for line in plan["lines"] if line["is_urgent"] and line["packs_to_buy"] == 0]
    can_wait = [
        line["name"]
        for line in plan["lines"]
        if not line["is_urgent"] and "no recent sales" not in line["reason"].lower()
    ]

    if not bought:
        if skipped_budget:
            return (
                f"{budget_str} ke budget mein zaroori items ka 1 pack bhi nahi aa sakta, "
                "isliye abhi koi order recommend nahi hua."
            )
        return (
            "Abhi kisi bhi product ko turant mangwane ki zaroorat nahi hai—sabhi items ka "
            "stock delivery tak kaafi hai ya recent sales zero hain."
        )

    bought_phrases = [
        f"{line['packs_to_buy']} {'pack' if line['packs_to_buy'] == 1 else 'packs'} {line['name']} "
        f"({format_rupees(line['cost'])})"
        for line in bought
    ]
    top_item = bought[0]
    gap_label = pluralize_days(top_item["gap_days"])

    parts = [
        f"{budget_str} ke budget mein sabse zaroori {top_item['name']} hai "
        f"({gap_label} ka stockout gap). Isliye {format_hinglish_list(bought_phrases)} "
        f"mangwana best rahega—total {format_rupees(plan['total_cost'])} kharch honge aur "
        f"{format_rupees(plan['remaining_budget'])} bachenge."
    ]
    if skipped_budget:
        skipped_names = format_hinglish_list([line["name"] for line in skipped_budget])
        parts.append(f"Bache hue budget mein {skipped_names} cover nahi ho paya.")
    if can_wait:
        parts.append(f"{format_hinglish_list(can_wait)} ka stock abhi chal jayega.")
    return " ".join(parts)


def build_ask_box_html(
    raw_ask: str,
    ask_reply: str | None,
    ask_error: str | None,
) -> str:
    """Render the Ask VyaparAI input box and Hinglish response or clarification prompt."""
    feedback_html = ""
    if ask_error:
        feedback_html = f'<div class="ask-error" role="alert">{escape(ask_error)}</div>'
    elif ask_reply:
        feedback_html = f'<div class="ask-reply" role="status"><strong>VyaparAI:</strong> {escape(ask_reply)}</div>'

    return f"""                <div class="ask-card">
                    <label for="ask_query">Ask VyaparAI</label>
                    <p class="ask-hint">Hinglish ya English mein budget puchhein, jaise: <em>"Mere paas ₹600 hain, kya mangwau?"</em></p>
                    <div class="input-row">
                        <input
                            id="ask_query"
                            class="ask-input"
                            name="ask"
                            type="text"
                            value="{escape(raw_ask)}"
                            placeholder="Mere paas ₹600 hain, kya mangwau?"
                        />
                        <button id="ask-submit-btn" type="submit" name="intent" value="ask">Ask VyaparAI</button>
                    </div>
                    {feedback_html}
                </div>"""


def evaluate_oil_supplier(
    key: str,
    title: str,
    raw_cost: str,
    raw_delivery: str,
    oil_product: Product | None,
    budget: Decimal | None,
) -> SupplierOptionResult:
    """Validate an Oil supplier option and calculate packs, cost, left, and stock gap."""
    cost, cost_error = parse_positive_cost(raw_cost)
    delivery, delivery_error = parse_non_negative_int(raw_delivery, "delivery days")

    packs_to_buy: int | None = None
    total_cost: Decimal | None = None
    money_left: Decimal | None = None
    gap_text: str | None = None
    gap_is_risk = False

    if (
        cost_error is None
        and delivery_error is None
        and cost is not None
        and delivery is not None
        and oil_product is not None
        and budget is not None
    ):
        packs_to_buy = int(budget // cost)
        total_cost = Decimal(packs_to_buy) * cost
        money_left = budget - total_cost

        daily_sales = oil_product["packs_sold_per_day"]
        if daily_sales <= 0:
            gap_text = "No recent sales (stock gap not estimated)"
        else:
            days_of_stock = float(Decimal(oil_product["packs_in_stock"]) / daily_sales)
            gap_days = delivery - days_of_stock
            if gap_days > 0:
                gap_is_risk = True
                gap_text = (
                    f"Possible {format_days(gap_days)}-day stock gap "
                    f"(stock lasts {pluralize_days(days_of_stock)} vs {pluralize_days(delivery)} delivery)"
                )
            else:
                gap_text = (
                    f"No expected stock gap "
                    f"(stock lasts {pluralize_days(days_of_stock)} vs {pluralize_days(delivery)} delivery)"
                )

    return {
        "key": key,
        "title": title,
        "raw_cost": raw_cost,
        "raw_delivery": raw_delivery,
        "cost_error": cost_error,
        "delivery_error": delivery_error,
        "packs_to_buy": packs_to_buy,
        "total_cost": total_cost,
        "money_left": money_left,
        "gap_text": gap_text,
        "gap_is_risk": gap_is_risk,
    }


def render_draft_editor_html(
    draft_message: str,
    textarea_id: str,
    copy_btn_id: str,
    whatsapp_btn_id: str,
    status_id: str,
    label_text: str = "Editable message",
) -> str:
    """Render an editable supplier message box with Copy and Open WhatsApp controls."""
    whatsapp_url = f"https://wa.me/?text={quote(draft_message)}"
    return f"""            <div class="draft-card">
                <label for="{textarea_id}">{escape(label_text)}</label>
                <textarea id="{textarea_id}" rows="3">{escape(draft_message)}</textarea>
                <div class="draft-actions">
                    <button type="button" id="{copy_btn_id}">Copy message</button>
                    <a
                        id="{whatsapp_btn_id}"
                        class="btn-whatsapp"
                        href="{escape(whatsapp_url)}"
                        target="_blank"
                        rel="noopener noreferrer"
                    >Open WhatsApp</a>
                    <span id="{status_id}" class="copy-status" aria-live="polite"></span>
                </div>
            </div>"""


def build_oil_comparison_html(
    row_inputs: list[ProductRowInput],
    budget: Decimal | None,
    params: Mapping[str, str],
) -> str:
    """Render the Compare Oil suppliers section below the regular order plan."""
    oil_row = next((row for row in row_inputs if row["name"].strip().lower() == "oil"), None)
    oil_product = oil_row["product"] if oil_row is not None else None

    raw_choice = params.get("set_oil_choice") or params.get("oil_choice", "")
    oil_choice = raw_choice if raw_choice in ("regular", "faster") else ""

    options: list[SupplierOptionResult] = []
    for key, title, default_cost, default_delivery in DEFAULT_OIL_SUPPLIERS:
        prefix = "oil_reg" if key == "regular" else "oil_fast"
        raw_cost = params.get(f"{prefix}_cost", default_cost)
        raw_delivery = params.get(f"{prefix}_delivery", default_delivery)
        options.append(
            evaluate_oil_supplier(
                key=key,
                title=title,
                raw_cost=raw_cost,
                raw_delivery=raw_delivery,
                oil_product=oil_product,
                budget=budget,
            )
        )

    prereq_notice = ""
    if oil_row is None:
        prereq_notice = '<div class="alert-error" role="alert">Add or upload an Oil product row above to compare Oil suppliers.</div>'
    elif oil_product is None:
        prereq_notice = '<div class="alert-error" role="alert">Fix Oil’s inventory values above to calculate supplier comparisons.</div>'
    elif budget is None:
        prereq_notice = '<div class="alert-error" role="alert">Enter a valid budget above to calculate supplier comparisons.</div>'

    cards_html: list[str] = []
    selected_option: SupplierOptionResult | None = None
    for opt in options:
        is_selected = opt["key"] == oil_choice
        if is_selected:
            selected_option = opt

        prefix = "oil_reg" if opt["key"] == "regular" else "oil_fast"
        cost_invalid_cls = " input-invalid" if opt["cost_error"] else ""
        delivery_invalid_cls = " input-invalid" if opt["delivery_error"] else ""
        cost_err_html = (
            f'<div class="field-error" role="alert">{escape(opt["cost_error"])}</div>'
            if opt["cost_error"]
            else ""
        )
        delivery_err_html = (
            f'<div class="field-error" role="alert">{escape(opt["delivery_error"])}</div>'
            if opt["delivery_error"]
            else ""
        )

        if opt["packs_to_buy"] is not None and opt["total_cost"] is not None and opt["money_left"] is not None and opt["gap_text"] is not None:
            pack_word = "pack" if opt["packs_to_buy"] == 1 else "packs"
            gap_badge_cls = "badge-warn" if opt["gap_is_risk"] else "badge-ok"
            metrics_html = f"""                        <dl class="compare-metrics">
                            <div><dt>Can buy</dt><dd><strong>{opt["packs_to_buy"]} {pack_word}</strong></dd></div>
                            <div><dt>Total cost</dt><dd><strong>{format_rupees(opt["total_cost"])}</strong></dd></div>
                            <div><dt>Money left</dt><dd><strong>{format_rupees(opt["money_left"])}</strong></dd></div>
                            <div><dt>Before delivery</dt><dd><span class="badge {gap_badge_cls} compare-gap-badge">{escape(opt["gap_text"])}</span></dd></div>
                        </dl>"""
            btn_disabled = ""
        else:
            metrics_html = '<p class="compare-unavailable">Fix invalid inputs above to see numbers for this supplier.</p>'
            btn_disabled = " disabled"

        selected_cls = " compare-card-selected" if is_selected else ""
        selected_tag = '<span class="badge badge-buy">Selected for Oil draft</span>' if is_selected else ""

        cards_html.append(
            f"""                <div class="compare-card{selected_cls}">
                    <div class="compare-card-head">
                        <h3>{escape(opt["title"])}</h3>
                        {selected_tag}
                    </div>
                    <div class="compare-inputs">
                        <div class="compare-field">
                            <label for="{prefix}_cost">Price per pack (₹)</label>
                            <div class="cell-prefix-wrap{cost_invalid_cls}">
                                <span class="cell-prefix" aria-hidden="true">₹</span>
                                <input class="table-input" id="{prefix}_cost" name="{prefix}_cost" type="text" inputmode="decimal" value="{escape(opt["raw_cost"])}" />
                            </div>
                            {cost_err_html}
                        </div>
                        <div class="compare-field">
                            <label for="{prefix}_delivery">Delivery days</label>
                            <input class="table-input standalone-input{delivery_invalid_cls}" id="{prefix}_delivery" name="{prefix}_delivery" type="text" inputmode="numeric" value="{escape(opt["raw_delivery"])}" />
                            {delivery_err_html}
                        </div>
                    </div>
{metrics_html}
                    <div class="compare-card-actions">
                        <button type="submit" name="set_oil_choice" value="{opt["key"]}" class="btn-secondary"{btn_disabled}>Use this option for Oil</button>
                    </div>
                </div>"""
        )

    oil_draft_html = ""
    if selected_option is not None and selected_option["packs_to_buy"] is not None:
        qty = selected_option["packs_to_buy"]
        if qty <= 0:
            oil_draft_html = (
                '<div class="oil-draft-wrap">'
                '<p class="draft-empty">Separate Oil-only order: No Oil packs can be bought under the current budget.</p>'
                "</div>"
            )
        else:
            pack_word = "pack" if qty == 1 else "packs"
            oil_msg = (
                f"Namaste, please send {qty} {pack_word} of Oil. "
                "Please confirm availability and delivery date."
            )
            editor_html = render_draft_editor_html(
                draft_message=oil_msg,
                textarea_id="oil-supplier-message",
                copy_btn_id="copy-oil-message-btn",
                whatsapp_btn_id="open-oil-whatsapp-btn",
                status_id="copy-oil-status",
                label_text=f"Separate Oil-only supplier draft ({selected_option['title']})",
            )
            oil_draft_html = f'<div class="oil-draft-wrap">{editor_html}</div>'

    cards_markup = "\n".join(cards_html)
    return f"""            <section class="compare-section" aria-label="Compare Oil suppliers">
                <input type="hidden" name="oil_choice" value="{escape(oil_choice)}" />
                <div class="planner-header">
                    <div class="compare-title-row">
                        <h2>Compare Oil suppliers</h2>
                        <span class="sample-tag">Separate Oil-Only Order</span>
                    </div>
                    <p class="section-note">Uses Oil’s current stock, daily sales, and current budget—separate from the regular order plan above.</p>
                </div>
                {prereq_notice}
                <div class="compare-grid">
{cards_markup}
                </div>
                {oil_draft_html}
            </section>"""


def render_home_html(
    query_params: Mapping[str, str] | None = None,
    *,
    row_inputs: list[ProductRowInput] | None = None,
    raw_budget: str | None = None,
    csv_errors: list[str] | None = None,
    csv_notice: str | None = None,
    inventory_label: str | None = None,
) -> str:
    """Render the complete single-page VyaparAI HTML document."""
    params = query_params or {}
    if raw_budget is None:
        raw_budget = params.get("budget", DEFAULT_BUDGET)
    if row_inputs is None:
        row_inputs = parse_product_rows(params)
    if inventory_label is None:
        inventory_label = params.get("inventory_label", "Sample Data (Editable)")

    raw_ask = params.get("ask", "")
    intent = params.get("intent", "manual")

    ask_reply: str | None = None
    ask_error: str | None = None
    asked_budget: Decimal | None = None
    if intent == "ask":
        asked_budget, ask_error = extract_budget_from_question(raw_ask)
        if ask_error is None and asked_budget is not None:
            raw_budget = format_decimal(asked_budget)

    has_product_errors = any(row["errors"] for row in row_inputs)

    budget, budget_error = parse_budget(raw_budget)
    products = [row["product"] for row in row_inputs if row["product"] is not None]

    plan: OrderPlan | None = None
    if not has_product_errors and budget is not None:
        plan = plan_order(products, budget)

    if intent == "ask" and ask_error is None and asked_budget is not None:
        ask_reply = build_hinglish_explanation(plan, has_product_errors, asked_budget)

    table_rows = build_table_rows(row_inputs)
    budget_invalid_cls = " input-invalid" if budget_error else ""
    budget_error_html = (
        f'<div class="field-error" role="alert">{escape(budget_error)}</div>' if budget_error else ""
    )
    csv_feedback_html = build_csv_feedback_html(csv_errors, csv_notice)
    ask_box_html = build_ask_box_html(raw_ask, ask_reply, ask_error)
    results_html = build_order_results_html(has_product_errors, budget_error, plan)
    oil_compare_html = build_oil_comparison_html(row_inputs, budget, params)
    draft_html = build_supplier_draft_html(plan)

    urgent_count = sum(
        1
        for p in products
        if (gap := calculate_stock_metrics(p)[1]) is not None and gap > 0
    )
    sidebar_budget = format_rupees(budget) if budget is not None else raw_budget

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>VyaparAI</title>
    <style>
        :root {{
            --mac-bg: linear-gradient(145deg, #e7edf6 0%, #eef2f7 48%, #dde5f0 100%);
            --mac-window: #ffffff;
            --mac-sidebar: rgba(245, 246, 248, 0.88);
            --mac-toolbar: rgba(248, 249, 251, 0.86);
            --mac-surface-subtle: #f5f5f7;
            --mac-surface-hover: rgba(0, 122, 255, 0.035);
            --mac-border: rgba(60, 60, 67, 0.14);
            --mac-border-strong: rgba(60, 60, 67, 0.24);
            --mac-text: #1d1d1f;
            --mac-text-secondary: #6e6e73;
            --mac-blue: #007aff;
            --mac-blue-hover: #0066d6;
            --mac-blue-soft: rgba(0, 122, 255, 0.12);
            --mac-green: #1e8e3e;
            --mac-green-hover: #177532;
            --mac-red: #d70015;
            --mac-red-bg: rgba(255, 59, 48, 0.12);
            --mac-green-bg: rgba(52, 199, 89, 0.15);
            --mac-gray-bg: rgba(142, 142, 147, 0.14);
            --mac-shadow: 0 24px 64px rgba(15, 23, 42, 0.13), 0 4px 14px rgba(15, 23, 42, 0.06);
        }}
        * {{
            box-sizing: border-box;
        }}
        html {{
            scroll-behavior: smooth;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Segoe UI", Roboto, sans-serif;
            margin: 0;
            min-height: 100vh;
            background: var(--mac-bg);
            color: var(--mac-text);
            padding: 2rem 1.25rem;
            -webkit-font-smoothing: antialiased;
            font-variant-numeric: tabular-nums;
        }}
        .mac-window {{
            max-width: 1220px;
            margin: 0 auto;
            background: var(--mac-window);
            border: 1px solid rgba(0, 0, 0, 0.1);
            border-radius: 14px;
            box-shadow: var(--mac-shadow);
            overflow: hidden;
        }}
        .mac-titlebar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 1rem;
            padding: 0.85rem 1.25rem;
            background: var(--mac-toolbar);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            border-bottom: 1px solid var(--mac-border);
        }}
        .titlebar-left {{
            display: flex;
            align-items: center;
            gap: 1rem;
        }}
        .traffic-lights {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 2px 4px;
        }}
        .traffic-dot {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
            border: 0.5px solid rgba(0, 0, 0, 0.15);
        }}
        .dot-close {{
            background: #ff5f57;
        }}
        .dot-min {{
            background: #febc2e;
        }}
        .dot-max {{
            background: #28c840;
        }}
        .titlebar-titles h1 {{
            font-size: 1.125rem;
            font-weight: 650;
            letter-spacing: -0.015em;
            margin: 0;
            line-height: 1.2;
        }}
        .subtitle {{
            font-size: 0.82rem;
            color: var(--mac-text-secondary);
            margin: 0.1rem 0 0 0;
        }}
        .banner-actions {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.5rem;
        }}
        .mac-shell {{
            display: grid;
            grid-template-columns: 232px minmax(0, 1fr);
            min-height: 680px;
        }}
        .mac-sidebar {{
            background: var(--mac-sidebar);
            border-right: 1px solid var(--mac-border);
            padding: 1.15rem 0.85rem;
            display: flex;
            flex-direction: column;
            gap: 1.35rem;
        }}
        .sidebar-group-title {{
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--mac-text-secondary);
            padding: 0 0.6rem;
            margin: 0 0 0.4rem 0;
        }}
        .sidebar-nav {{
            list-style: none;
            margin: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            gap: 0.2rem;
        }}
        .sidebar-nav a {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.48rem 0.65rem;
            border-radius: 7px;
            font-size: 0.88rem;
            font-weight: 500;
            color: var(--mac-text);
            text-decoration: none;
            transition: background-color 0.12s ease;
        }}
        .sidebar-nav a:hover {{
            background: rgba(0, 0, 0, 0.05);
        }}
        .sidebar-nav a.is-primary {{
            background: var(--mac-blue-soft);
            color: #005ecb;
            font-weight: 600;
        }}
        .sidebar-pill {{
            font-size: 0.72rem;
            font-weight: 600;
            padding: 0.1rem 0.45rem;
            border-radius: 999px;
            background: rgba(60, 60, 67, 0.1);
            color: var(--mac-text-secondary);
        }}
        .sidebar-stats {{
            background: rgba(255, 255, 255, 0.72);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 0.75rem;
            display: flex;
            flex-direction: column;
            gap: 0.6rem;
        }}
        .sidebar-stat-row {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 0.5rem;
            font-size: 0.8rem;
        }}
        .sidebar-stat-row span {{
            color: var(--mac-text-secondary);
        }}
        .sidebar-stat-row strong {{
            color: var(--mac-text);
            font-weight: 600;
        }}
        .sidebar-source-tag {{
            font-size: 0.75rem;
            color: var(--mac-text-secondary);
            line-height: 1.35;
            padding-top: 0.35rem;
            border-top: 1px solid var(--mac-border);
            word-break: break-word;
        }}
        .mac-content {{
            padding: 1.6rem 1.85rem 2.25rem;
            background: var(--mac-window);
        }}
        section + section,
        #inventory-form > section {{
            margin-top: 2rem;
        }}
        h2 {{
            font-size: 1.18rem;
            font-weight: 650;
            letter-spacing: -0.015em;
            margin: 0 0 0.25rem 0;
            color: var(--mac-text);
        }}
        .section-note {{
            font-size: 0.88rem;
            color: var(--mac-text-secondary);
            margin: 0;
            line-height: 1.45;
        }}
        .sample-banner {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 0.75rem;
            margin-bottom: 0.75rem;
        }}
        .sample-tag {{
            font-size: 0.74rem;
            font-weight: 600;
            letter-spacing: 0.02em;
            color: var(--mac-text-secondary);
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            padding: 0.24rem 0.65rem;
            border-radius: 999px;
        }}
        .table-wrap {{
            background: #ffffff;
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.88rem;
        }}
        th, td {{
            padding: 0.75rem 0.85rem;
            border-bottom: 1px solid rgba(60, 60, 67, 0.09);
            vertical-align: top;
        }}
        th {{
            background-color: #f9f9fb;
            font-weight: 600;
            color: var(--mac-text-secondary);
            font-size: 0.78rem;
            letter-spacing: 0.01em;
        }}
        tbody tr:hover {{
            background-color: var(--mac-surface-hover);
        }}
        tbody tr:last-child td {{
            border-bottom: none;
        }}
        .nowrap {{
            white-space: nowrap;
        }}
        .table-input {{
            width: 78px;
            padding: 0.36rem 0.5rem;
            font-size: 0.88rem;
            color: var(--mac-text);
            background: #ffffff;
            font-family: inherit;
        }}
        .standalone-input {{
            border: 1px solid var(--mac-border-strong);
            border-radius: 6px;
        }}
        .standalone-input:focus {{
            outline: none;
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.2);
        }}
        .cell-prefix-wrap {{
            display: inline-flex;
            align-items: center;
            border: 1px solid var(--mac-border-strong);
            border-radius: 6px;
            background: #ffffff;
            overflow: hidden;
        }}
        .cell-prefix-wrap:focus-within {{
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.2);
        }}
        .cell-prefix-wrap .table-input {{
            border: none;
            outline: none;
            width: 70px;
        }}
        .cell-prefix {{
            padding: 0.36rem 0.45rem;
            background: var(--mac-surface-subtle);
            color: var(--mac-text-secondary);
            font-weight: 600;
            font-size: 0.82rem;
            border-right: 1px solid var(--mac-border);
        }}
        .input-invalid {{
            border-color: #ff3b30 !important;
            background-color: #fff5f5;
        }}
        .field-error {{
            margin-top: 0.3rem;
            font-size: 0.76rem;
            color: var(--mac-red);
            font-weight: 500;
            line-height: 1.3;
            max-width: 190px;
        }}
        .budget-controls .field-error {{
            max-width: none;
        }}
        .badge {{
            display: inline-block;
            font-size: 0.76rem;
            font-weight: 600;
            padding: 0.22rem 0.58rem;
            border-radius: 999px;
            white-space: nowrap;
        }}
        .badge-warn {{
            background-color: var(--mac-red-bg);
            color: var(--mac-red);
        }}
        .badge-ok {{
            background-color: var(--mac-green-bg);
            color: #157f37;
        }}
        .badge-neutral {{
            background-color: var(--mac-gray-bg);
            color: #48484a;
        }}
        .badge-error {{
            background-color: var(--mac-red-bg);
            color: var(--mac-red);
        }}
        .badge-buy {{
            background-color: var(--mac-blue-soft);
            color: #005ecb;
        }}
        .badge-skip {{
            background-color: var(--mac-gray-bg);
            color: var(--mac-text-secondary);
        }}
        .detail, .reason-cell {{
            font-size: 0.83rem;
            color: var(--mac-text-secondary);
            line-height: 1.42;
        }}
        .detail {{
            margin-top: 0.35rem;
        }}
        .planner-section {{
            margin-top: 2rem;
            padding-top: 1.6rem;
            border-top: 1px solid var(--mac-border);
        }}
        .planner-header {{
            margin-bottom: 0.9rem;
        }}
        .budget-controls {{
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 1rem 1.15rem;
            margin-bottom: 0.9rem;
        }}
        .budget-controls label {{
            display: block;
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--mac-text);
            margin-bottom: 0.45rem;
        }}
        .input-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.65rem;
            align-items: center;
        }}
        .rupee-input {{
            display: flex;
            align-items: center;
            border: 1px solid var(--mac-border-strong);
            border-radius: 8px;
            background: #ffffff;
            overflow: hidden;
            width: 210px;
        }}
        .rupee-input:focus-within {{
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.2);
        }}
        .rupee-prefix {{
            padding: 0.48rem 0.65rem;
            background: #f9f9fb;
            color: var(--mac-text-secondary);
            font-weight: 600;
            font-size: 0.9rem;
            border-right: 1px solid var(--mac-border);
        }}
        .rupee-input input {{
            border: none;
            outline: none;
            padding: 0.48rem 0.7rem;
            font-size: 0.95rem;
            width: 100%;
            color: var(--mac-text);
            background: transparent;
            font-family: inherit;
        }}
        button, .btn-secondary {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 7px;
            padding: 0.48rem 0.95rem;
            font-size: 0.86rem;
            font-weight: 500;
            font-family: inherit;
            cursor: pointer;
            text-decoration: none;
            line-height: 1.25;
            transition: background-color 0.12s ease, border-color 0.12s ease, transform 0.06s ease;
        }}
        button {{
            background-color: var(--mac-blue);
            color: #ffffff;
            border: 1px solid rgba(0, 0, 0, 0.06);
            box-shadow: 0 1px 2px rgba(0, 122, 255, 0.22);
        }}
        button:hover {{
            background-color: var(--mac-blue-hover);
        }}
        button:active {{
            transform: scale(0.99);
        }}
        button:disabled {{
            opacity: 0.45;
            cursor: not-allowed;
        }}
        .btn-secondary {{
            background-color: #ffffff;
            color: var(--mac-text);
            border: 1px solid var(--mac-border-strong);
            box-shadow: 0 1px 1px rgba(0, 0, 0, 0.04);
        }}
        .btn-secondary:hover {{
            background-color: #f5f5f7;
        }}
        .alert-error {{
            background-color: #fff5f5;
            border: 1px solid rgba(255, 59, 48, 0.28);
            color: var(--mac-red);
            border-radius: 10px;
            padding: 0.8rem 1rem;
            margin-bottom: 0.9rem;
            font-size: 0.88rem;
            font-weight: 500;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 0.75rem;
            margin-bottom: 0.9rem;
        }}
        .summary-card {{
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 0.8rem 1rem;
        }}
        .summary-label {{
            display: block;
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--mac-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 0.2rem;
        }}
        .summary-value {{
            font-size: 1.28rem;
            font-weight: 650;
            letter-spacing: -0.02em;
            color: var(--mac-text);
        }}
        .draft-section {{
            margin-top: 2rem;
            padding-top: 1.6rem;
            border-top: 1px solid var(--mac-border);
        }}
        .draft-card {{
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 1rem 1.15rem;
        }}
        .draft-card label {{
            display: block;
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--mac-text);
            margin-bottom: 0.45rem;
        }}
        .draft-card textarea {{
            width: 100%;
            padding: 0.65rem 0.8rem;
            font-family: inherit;
            font-size: 0.9rem;
            line-height: 1.48;
            color: var(--mac-text);
            background: #ffffff;
            border: 1px solid var(--mac-border-strong);
            border-radius: 8px;
            resize: vertical;
        }}
        .draft-card textarea:focus {{
            outline: none;
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.2);
        }}
        .draft-actions {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.6rem;
            margin-top: 0.75rem;
        }}
        .btn-whatsapp {{
            display: inline-flex;
            align-items: center;
            background-color: var(--mac-green);
            color: #ffffff;
            border-radius: 7px;
            padding: 0.48rem 0.95rem;
            font-size: 0.86rem;
            font-weight: 500;
            text-decoration: none;
            line-height: 1.25;
            box-shadow: 0 1px 2px rgba(30, 142, 62, 0.22);
        }}
        .btn-whatsapp:hover {{
            background-color: var(--mac-green-hover);
        }}
        .copy-status {{
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--mac-green);
        }}
        .draft-empty {{
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 0.9rem 1.1rem;
            color: var(--mac-text-secondary);
            font-size: 0.88rem;
            margin: 0;
        }}
        .csv-toolbar {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            background: var(--mac-surface-subtle);
            border: 1px solid var(--mac-border);
            border-radius: 10px;
            padding: 0.7rem 0.95rem;
            margin-bottom: 0.85rem;
        }}
        .csv-upload-controls {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.65rem;
        }}
        .csv-upload-controls label {{
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--mac-text);
        }}
        .csv-file-input {{
            font-size: 0.82rem;
            color: var(--mac-text-secondary);
            font-family: inherit;
        }}
        .csv-alert {{
            margin-top: 0;
            margin-bottom: 0.85rem;
        }}
        .error-list {{
            margin: 0.4rem 0 0 1.15rem;
            padding: 0;
            font-weight: 400;
        }}
        .error-list li {{
            margin-bottom: 0.2rem;
        }}
        .alert-success {{
            background-color: #f0fbf3;
            border: 1px solid rgba(52, 199, 89, 0.35);
            color: #157f37;
            border-radius: 10px;
            padding: 0.7rem 0.95rem;
            font-size: 0.86rem;
            font-weight: 500;
        }}
        .ask-card {{
            background: linear-gradient(180deg, #fafbfd 0%, #f4f6fa 100%);
            border: 1px solid var(--mac-border);
            border-radius: 11px;
            padding: 1rem 1.15rem;
            margin-bottom: 0.95rem;
        }}
        .ask-card label {{
            display: block;
            font-size: 0.88rem;
            font-weight: 650;
            color: var(--mac-text);
            margin-bottom: 0.2rem;
        }}
        .ask-hint {{
            font-size: 0.82rem;
            color: var(--mac-text-secondary);
            margin: 0 0 0.6rem 0;
        }}
        .ask-input {{
            flex: 1 1 300px;
            min-width: 210px;
            border: 1px solid var(--mac-border-strong);
            border-radius: 8px;
            padding: 0.5rem 0.75rem;
            font-size: 0.92rem;
            color: var(--mac-text);
            background: #ffffff;
            font-family: inherit;
        }}
        .ask-input:focus {{
            outline: none;
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.2);
        }}
        .ask-reply {{
            margin-top: 0.75rem;
            background-color: rgba(0, 122, 255, 0.08);
            border: 1px solid rgba(0, 122, 255, 0.22);
            color: #004999;
            border-radius: 8px;
            padding: 0.72rem 0.95rem;
            font-size: 0.88rem;
            line-height: 1.48;
        }}
        .ask-error {{
            margin-top: 0.75rem;
            background-color: #fff5f5;
            border: 1px solid rgba(255, 59, 48, 0.28);
            color: var(--mac-red);
            border-radius: 8px;
            padding: 0.72rem 0.95rem;
            font-size: 0.86rem;
            font-weight: 500;
        }}
        .compare-section {{
            margin-top: 2rem;
            padding-top: 1.6rem;
            border-top: 1px solid var(--mac-border);
        }}
        .compare-title-row {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.6rem;
            margin-bottom: 0.25rem;
        }}
        .compare-title-row h2 {{
            margin: 0;
        }}
        .compare-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
            gap: 0.95rem;
        }}
        .compare-card {{
            background: #ffffff;
            border: 1px solid var(--mac-border);
            border-radius: 11px;
            padding: 1.05rem 1.15rem;
            display: flex;
            flex-direction: column;
            gap: 0.85rem;
        }}
        .compare-card-selected {{
            border-color: var(--mac-blue);
            box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.18);
        }}
        .compare-card-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
        }}
        .compare-card-head h3 {{
            margin: 0;
            font-size: 1rem;
            font-weight: 650;
            color: var(--mac-text);
        }}
        .compare-inputs {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.8rem;
        }}
        .compare-field label {{
            display: block;
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--mac-text-secondary);
            margin-bottom: 0.28rem;
        }}
        .compare-metrics {{
            margin: 0;
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.6rem;
            padding: 0.7rem 0.75rem;
            background: var(--mac-surface-subtle);
            border-radius: 8px;
            border: 1px solid rgba(60, 60, 67, 0.08);
        }}
        .compare-metrics > div:last-child {{
            grid-column: 1 / -1;
        }}
        .compare-metrics dt {{
            font-size: 0.7rem;
            font-weight: 600;
            color: var(--mac-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 0.15rem;
        }}
        .compare-metrics dd {{
            margin: 0;
            font-size: 0.9rem;
            color: var(--mac-text);
        }}
        .compare-gap-badge {{
            white-space: normal;
            line-height: 1.35;
        }}
        .compare-unavailable {{
            margin: 0;
            font-size: 0.84rem;
            color: var(--mac-text-secondary);
        }}
        .compare-card-actions {{
            margin-top: auto;
        }}
        .oil-draft-wrap {{
            margin-top: 0.95rem;
        }}
        @media (max-width: 900px) {{
            body {{
                padding: 0.85rem;
            }}
            .mac-shell {{
                grid-template-columns: 1fr;
            }}
            .mac-sidebar {{
                border-right: none;
                border-bottom: 1px solid var(--mac-border);
                padding: 0.9rem 1rem;
            }}
            .sidebar-nav {{
                flex-direction: row;
                flex-wrap: wrap;
            }}
            .mac-content {{
                padding: 1.2rem 1rem 1.6rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="mac-window">
        <header class="mac-titlebar">
            <div class="titlebar-left">
                <div class="traffic-lights" aria-hidden="true">
                    <span class="traffic-dot dot-close"></span>
                    <span class="traffic-dot dot-min"></span>
                    <span class="traffic-dot dot-max"></span>
                </div>
                <div class="titlebar-titles">
                    <h1>VyaparAI</h1>
                    <p class="subtitle">Your kirana restocking assistant.</p>
                </div>
            </div>
            <div class="banner-actions">
                <a class="btn-secondary" href="/sample-inventory.csv" download="sample_inventory.csv">Download sample CSV</a>
                <a class="btn-secondary" href="/">Reset sample data</a>
            </div>
        </header>
        <div class="mac-shell">
            <aside class="mac-sidebar" aria-label="Workspace navigation and overview">
                <div>
                    <p class="sidebar-group-title">Workspace</p>
                    <ul class="sidebar-nav">
                        <li><a href="#section-inventory" class="is-primary"><span>Inventory</span><span class="sidebar-pill">{len(row_inputs)}</span></a></li>
                        <li><a href="#section-planner"><span>Plan my order</span></a></li>
                        <li><a href="#section-ask"><span>Ask VyaparAI</span></a></li>
                        <li><a href="#section-compare"><span>Compare Oil suppliers</span></a></li>
                        <li><a href="#section-draft"><span>Supplier message</span></a></li>
                    </ul>
                </div>
                <div>
                    <p class="sidebar-group-title">Snapshot</p>
                    <div class="sidebar-stats">
                        <div class="sidebar-stat-row"><span>Products</span><strong>{len(row_inputs)}</strong></div>
                        <div class="sidebar-stat-row"><span>Restock soon</span><strong>{urgent_count}</strong></div>
                        <div class="sidebar-stat-row"><span>Budget</span><strong>{escape(sidebar_budget)}</strong></div>
                        <div class="sidebar-source-tag">{escape(inventory_label)}</div>
                    </div>
                </div>
            </aside>
            <main class="mac-content">
                <section id="section-inventory" aria-label="Sample product inventory">
                    <div class="sample-banner">
                        <div>
                            <h2>Inventory</h2>
                            <p class="section-note">Edit stock, daily sales, delivery days, and wholesale cost, or upload a 5-column CSV.</p>
                        </div>
                        <span class="sample-tag">{escape(inventory_label)}</span>
                    </div>
                    <form id="csv-upload-form" class="csv-toolbar" method="post" action="/upload-csv" enctype="multipart/form-data">
                        <div class="csv-upload-controls">
                            <label for="csv_file">Inventory CSV</label>
                            <input
                                id="csv_file"
                                class="csv-file-input"
                                name="csv_file"
                                type="file"
                                accept=".csv,text/csv"
                            />
                        </div>
                        <button type="submit" class="btn-secondary">Upload inventory CSV</button>
                    </form>
                    {csv_feedback_html}
                </section>
                <form id="inventory-form" method="get" action="/" autocomplete="off">
                    <input type="hidden" name="row_count" value="{len(row_inputs)}" />
                    <input type="hidden" name="inventory_label" value="{escape(inventory_label)}" />
                    <section aria-label="Editable product inventory table">
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Product</th>
                                        <th>Packs in stock</th>
                                        <th>Packs sold per day</th>
                                        <th>Supplier delivery days</th>
                                        <th>Wholesale cost per pack</th>
                                        <th>Days of stock</th>
                                        <th>Restock status</th>
                                    </tr>
                                </thead>
                                <tbody>
{table_rows}
                                </tbody>
                            </table>
                        </div>
                    </section>
                    <section id="section-planner" class="planner-section" aria-label="Plan my order">
                        <div class="planner-header">
                            <h2>Plan my order</h2>
                            <p class="section-note">Prioritizes products marked <strong>Restock soon</strong> by largest stockout gap, aiming for 2 days of sales per product within your budget.</p>
                        </div>
                        <div class="budget-controls">
                            <label for="budget">Budget (₹)</label>
                            <div class="input-row">
                                <div class="rupee-input{budget_invalid_cls}">
                                    <span class="rupee-prefix" aria-hidden="true">₹</span>
                                    <input
                                        id="budget"
                                        name="budget"
                                        type="text"
                                        inputmode="decimal"
                                        value="{escape(raw_budget)}"
                                        placeholder="1000"
                                    />
                                </div>
                                <button id="manual-submit-btn" type="submit" name="intent" value="manual">Recommend order</button>
                            </div>
                            {budget_error_html}
                        </div>
                        <div id="section-ask">
{ask_box_html}
                        </div>
                        {results_html}
                    </section>
                    <div id="section-compare">
{oil_compare_html}
                    </div>
                </form>
                <div id="section-draft">
{draft_html}
                </div>
            </main>
        </div>
    </div>
    <script>
        (function () {{
            const askInput = document.getElementById("ask_query");
            const askBtn = document.getElementById("ask-submit-btn");
            if (askInput && askBtn) {{
                askInput.addEventListener("keydown", function (event) {{
                    if (event.key === "Enter") {{
                        event.preventDefault();
                        askBtn.click();
                    }}
                }});
            }}

            const uploadForm = document.getElementById("csv-upload-form");
            const inventoryForm = document.getElementById("inventory-form");
            if (uploadForm && inventoryForm) {{
                uploadForm.addEventListener("submit", function () {{
                    uploadForm.querySelectorAll("input[data-synced-field]").forEach(function (el) {{
                        el.remove();
                    }});
                    const formData = new FormData(inventoryForm);
                    formData.forEach(function (value, key) {{
                        const hidden = document.createElement("input");
                        hidden.type = "hidden";
                        hidden.name = key;
                        hidden.value = String(value);
                        hidden.setAttribute("data-synced-field", "1");
                        uploadForm.appendChild(hidden);
                    }});
                }});
            }}

            function setupDraftControls(textareaId, copyBtnId, whatsappBtnId, statusId) {{
                const textarea = document.getElementById(textareaId);
                const copyBtn = document.getElementById(copyBtnId);
                const whatsappLink = document.getElementById(whatsappBtnId);
                const copyStatus = document.getElementById(statusId);

                if (!textarea) {{
                    return;
                }}

                textarea.addEventListener("input", function () {{
                    if (whatsappLink) {{
                        whatsappLink.href = "https://wa.me/?text=" + encodeURIComponent(textarea.value);
                    }}
                    if (copyStatus) {{
                        copyStatus.textContent = "";
                    }}
                }});

                if (copyBtn) {{
                    copyBtn.addEventListener("click", async function () {{
                        try {{
                            if (navigator.clipboard && navigator.clipboard.writeText) {{
                                await navigator.clipboard.writeText(textarea.value);
                            }} else {{
                                textarea.select();
                                document.execCommand("copy");
                            }}
                            if (copyStatus) {{
                                copyStatus.textContent = "Copied!";
                            }}
                        }} catch (err) {{
                            textarea.select();
                            document.execCommand("copy");
                            if (copyStatus) {{
                                copyStatus.textContent = "Copied!";
                            }}
                        }}
                    }});
                }}
            }}

            setupDraftControls("supplier-message", "copy-message-btn", "open-whatsapp-btn", "copy-status");
            setupDraftControls("oil-supplier-message", "copy-oil-message-btn", "open-oil-whatsapp-btn", "copy-oil-status");
        }})();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
@app.get("/api/", response_class=HTMLResponse)
@app.get("/api/index", response_class=HTMLResponse)
@app.get("/api/index.py", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """Serve the VyaparAI home page with editable inventory, CSV tools, and order planner."""
    return HTMLResponse(content=render_home_html(request.query_params))


@app.get("/sample-inventory.csv")
@app.get("/api/sample-inventory.csv")
@app.get("/api/index.py/sample-inventory.csv")
def download_sample_csv() -> Response:
    """Download the built-in sample inventory as a 5-column CSV file."""
    return Response(
        content=generate_sample_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sample_inventory.csv"'},
    )


@app.post("/upload-csv", response_class=HTMLResponse)
@app.post("/api/upload-csv", response_class=HTMLResponse)
@app.post("/api/index.py/upload-csv", response_class=HTMLResponse)
async def upload_inventory_csv(request: Request) -> HTMLResponse:
    """Validate an uploaded inventory CSV and populate the table, or preserve current rows on error."""
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    fields, uploaded_file = parse_multipart_form(content_type, body)

    raw_budget = fields.get("budget", request.query_params.get("budget", DEFAULT_BUDGET))
    current_label = fields.get("inventory_label", "Sample Data (Editable)")
    current_rows = parse_product_rows(fields)

    if uploaded_file is None or (not uploaded_file[0] and not uploaded_file[1]):
        return HTMLResponse(
            content=render_home_html(
                fields,
                row_inputs=current_rows,
                raw_budget=raw_budget,
                csv_errors=["Please choose a CSV file to upload."],
                inventory_label=current_label,
            )
        )

    filename, csv_bytes = uploaded_file
    parsed_rows, csv_errors = parse_inventory_csv(csv_bytes)
    if csv_errors or parsed_rows is None:
        return HTMLResponse(
            content=render_home_html(
                fields,
                row_inputs=current_rows,
                raw_budget=raw_budget,
                csv_errors=csv_errors,
                inventory_label=current_label,
            )
        )

    label = filename or "CSV"
    return HTMLResponse(
        content=render_home_html(
            fields,
            row_inputs=parsed_rows,
            raw_budget=raw_budget,
            csv_notice=f"Loaded {len(parsed_rows)} product(s) from {label}.",
            inventory_label=f"Uploaded CSV: {label} (Editable)",
        )
    )
