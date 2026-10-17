#!/usr/bin/env python3

import base64
import csv
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Callable, NoReturn, Optional
from urllib.parse import unquote

import typer
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateError,
    select_autoescape,
)
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# --------------------------------------------------------------------------- #
# Settings - tweak these to taste
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = BASE_DIR / "templates" / "index.html"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"

NUMBER_FORMAT = "ER-{year}-{seq:04d}"
DATE_DISPLAY_FORMAT = "%d %B %Y"
DATE_INPUT_FORMATS = (
    DATE_DISPLAY_FORMAT,
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d %b %Y",
)

CURRENCY_MAJOR = "Taka"
CURRENCY_MINOR = "Poisha"
PDF_MARGIN = "12mm"
MAX_VALUE = Decimal("1000000000")

COUNTER_FILE = BASE_DIR / ".report_counter.json"

app = typer.Typer(
    help="Welcome to RCY expense report generator!",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(highlight=False)

TWO_PLACES = Decimal("0.01")


def fail(message: str) -> NoReturn:
    console.print(f"[bold red]Error:[/] {escape(message)}")
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Money, numbers and dates
# --------------------------------------------------------------------------- #
def money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    return f"{value:,.2f}"


def fmt_qty(value: Decimal) -> str:
    """100 -> '100', 2.50 -> '2.5'"""
    return format(value.normalize(), "f")


def json_number(value: Decimal):
    return int(value) if value == value.to_integral_value() else float(value)


def parse_positive(text, field: str, *, as_money: bool = False) -> Decimal:
    """Parse a user-typed number ('1,250.50' is fine). Raises ValueError with a friendly message."""
    try:
        value = Decimal(str(text).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{field} must be a number") from None
    if not value.is_finite():
        raise ValueError(f"{field} must be a number")
    if value > MAX_VALUE:
        raise ValueError(f"{field} is too large")
    if as_money:
        value = money(value)
    if value <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return value


def parse_date(text: str) -> date:
    text = str(text).strip()
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Couldn't read that date - try DD-MM-YYYY, e.g. 15-10-2026")


def require_text(text: str) -> str:
    text = str(text).strip()
    if not text:
        raise ValueError("This can't be empty")
    return text


def parse_choice(text: str, choices: str) -> str:
    text = str(text).strip().lower()
    if len(text) == 1 and text in choices:
        return text
    raise ValueError(f"Please type one of: {', '.join(choices)}")


def parse_index(text: str, count: int) -> int:
    try:
        n = int(str(text).strip())
    except ValueError:
        raise ValueError("Please enter a number") from None
    if not 1 <= n <= count:
        raise ValueError(f"Please enter a number between 1 and {count}")
    return n


_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words_below_1000(n: int) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        parts.append(_TENS[tens] + (f"-{_ONES[ones]}" if ones else ""))
    elif rest:
        parts.append(_ONES[rest])
    return " ".join(parts)


def int_to_words(n: int) -> str:
    """South-Asian grouping: Crore / Lakh / Thousand."""
    if n == 0:
        return "Zero"
    parts = []
    for size, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        chunk, n = divmod(n, size)
        if chunk:
            parts.append(f"{int_to_words(chunk)} {label}")
    if n:
        parts.append(_words_below_1000(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    major, minor = divmod(int(money(amount) * 100), 100)
    words = f"{int_to_words(major)} {CURRENCY_MAJOR}"
    if minor:
        words += f" and {int_to_words(minor)} {CURRENCY_MINOR}"
    return f"{words} Only"


def prompt_proc(parser: Callable, *args, **kwargs) -> Callable:
    """Wrap a parser so a ValueError becomes a re-prompt with an error message."""

    def proc(text):
        try:
            return parser(text, *args, **kwargs)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    return proc


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    name: str
    quantity: Decimal
    rate: Decimal
    description: str = ""

    @property
    def amount(self) -> Decimal:
        return money(self.quantity * self.rate)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "quantity": json_number(self.quantity),
            "rate": json_number(self.rate),
        }

    @classmethod
    def from_mapping(cls, row: dict, where: str) -> "Item":
        """Build an Item from a CSV row / JSON object (accepts a few column aliases)."""
        if not isinstance(row, dict):
            raise ValueError(f"{where}: expected an object with name, quantity, rate")
        row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}

        def pick(*keys):
            for key in keys:
                if row.get(key) not in (None, ""):
                    return row[key]
            return None

        name = str(pick("name", "item") or "").strip()
        if not name:
            raise ValueError(f"{where}: missing item name")
        qty_raw = pick("quantity", "qty")
        try:
            quantity = parse_positive(1 if qty_raw is None else qty_raw, "quantity")
            rate = parse_positive(pick("rate", "price", "unit_price"), "rate", as_money=True)
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from None
        description = str(pick("description", "desc") or "").strip().replace("\\n", "\n")
        return cls(name=name, quantity=quantity, rate=rate, description=description)


@dataclass
class Report:
    number: str
    report_date: date
    items: list

    @property
    def total(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal("0.00"))


def load_items(path: Path) -> list:
    """Load items from .csv (columns: name, description, quantity, rate) or .json."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = [(f"line {n}", row) for n, row in enumerate(csv.DictReader(fh), start=2)]
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
            if isinstance(data, dict):
                data = data.get("items", [])
            if not isinstance(data, list):
                fail(f"{path.name}: expected a list of items")
            rows = [(f"item {n}", row) for n, row in enumerate(data, start=1)]
        else:
            fail("--items-file must be a .csv or .json file")
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
        fail(f"Couldn't read {path.name}: {exc}")
    try:
        return [Item.from_mapping(row, where) for where, row in rows]
    except ValueError as exc:
        fail(f"{path.name} - {exc}")


def save_report_json(path: Path, report: Report) -> None:
    payload = {
        "number": report.number,
        "date": report.report_date.isoformat(),
        "items": [item.to_json() for item in report.items],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_report(path: Path) -> Report:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError) as exc:
        fail(f"Couldn't read {path.name}: {exc}")
    if not isinstance(data, dict) or not {"number", "date", "items"} <= data.keys():
        fail(f"{path.name} must be a JSON object with 'number', 'date' and 'items'")
    try:
        report_date = parse_date(data["date"])
    except ValueError as exc:
        fail(f"{path.name}: {exc}")
    items_path_label = path.name
    if not isinstance(data["items"], list):
        fail(f"{items_path_label}: 'items' must be a list")
    try:
        items = [
            Item.from_mapping(row, f"item {n}") for n, row in enumerate(data["items"], start=1)
        ]
    except ValueError as exc:
        fail(f"{path.name} - {exc}")
    if not items:
        fail(f"{path.name} has no items")
    return Report(number=str(data["number"]).strip(), report_date=report_date, items=items)


# --------------------------------------------------------------------------- #
# Report numbering (ER-2026-0001, ER-2026-0002, ...)
# --------------------------------------------------------------------------- #
def _read_counter() -> dict:
    try:
        data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def next_report_number(year: int) -> str:
    seq = int(_read_counter().get(str(year), 0)) + 1
    return NUMBER_FORMAT.format(year=year, seq=seq)


def commit_report_number(year: int) -> None:
    counter = _read_counter()
    counter[str(year)] = int(counter.get(str(year), 0)) + 1
    COUNTER_FILE.write_text(json.dumps(counter, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Interactive entry
# --------------------------------------------------------------------------- #
def prompt_item(existing: Optional[Item] = None, *, allow_finish: bool = False) -> Optional[Item]:
    """Prompt for one item. With `existing`, Enter keeps the current value of each field.
    With `allow_finish`, a blank name returns None (= done adding items)."""
    editing = existing is not None

    if allow_finish:
        name = typer.prompt("  Name (blank to finish)", default="", show_default=False).strip()
        if not name:
            return None
    else:
        name = typer.prompt(
            "  Name",
            default=existing.name if editing else None,
            value_proc=prompt_proc(require_text),
        )

    has_desc = editing and bool(existing.description)
    description = typer.prompt(
        "  Description (optional, - to clear)" if has_desc else "  Description (optional)",
        default=existing.description if editing else "",
        show_default=has_desc,
        value_proc=lambda s: s.strip(),
    )
    if description == "-":
        description = ""
    description = description.replace("\\n", "\n")  # typing \n makes a line break

    quantity = typer.prompt(
        "  Quantity",
        default=fmt_qty(existing.quantity) if editing else "1",
        value_proc=prompt_proc(parse_positive, "Quantity"),
    )
    rate = typer.prompt(
        "  Rate",
        default=f"{existing.rate:.2f}" if editing else None,
        value_proc=prompt_proc(parse_positive, "Rate", as_money=True),
    )

    item = Item(name=name, quantity=quantity, rate=rate, description=description)
    console.print(f"  [dim]-> amount {fmt_money(item.amount)}[/]")
    return item


def collect_items(items: list) -> None:
    while True:
        console.print(f"\n[bold cyan]Item {len(items) + 1}[/]")
        item = prompt_item(allow_finish=bool(items))
        if item is None:
            return
        items.append(item)


def show_report(report: Report) -> None:
    title = f"{report.number} - {report.report_date.strftime(DATE_DISPLAY_FORMAT)}"
    table = Table(title=title, title_style="bold", header_style="bold", show_footer=True)
    table.add_column("#", justify="right")
    table.add_column("Item", overflow="fold", ratio=1)
    table.add_column("Qty", justify="right")
    table.add_column("Rate", justify="right", footer="[bold]Total[/]")
    table.add_column("Amount", justify="right", footer=f"[bold]{fmt_money(report.total)}[/]")
    for n, item in enumerate(report.items, start=1):
        label = escape(item.name)
        if item.description:
            label += f"\n[dim]{escape(item.description)}[/]"
        table.add_row(
            str(n), label, fmt_qty(item.quantity), fmt_money(item.rate), fmt_money(item.amount)
        )
    console.print()
    console.print(table)
    if report.items:
        console.print(f"[dim]In words:[/] {amount_in_words(report.total)}")


def ask_index(count: int, verb: str) -> int:
    n = typer.prompt(
        f"Item number to {verb}",
        default="1" if count == 1 else None,
        value_proc=prompt_proc(parse_index, count),
    )
    return n - 1


def review(report: Report) -> None:
    """Show the items and let the user add / edit / delete until they're happy."""
    menu = "(g)enerate  (a)dd  (e)dit  (d)elete  (q)uit"
    while True:
        show_report(report)
        action = typer.prompt(
            menu,
            default="g",
            value_proc=prompt_proc(parse_choice, "gaedq"),
        )

        if action == "g":
            if report.items:
                return
            console.print("[yellow]Add at least one item first.[/]")
        elif action == "a":
            collect_items(report.items)
        elif action in ("e", "d") and not report.items:
            console.print("[yellow]There are no items yet.[/]")
        elif action == "e":
            idx = ask_index(len(report.items), "edit")
            report.items[idx] = prompt_item(existing=report.items[idx])
        elif action == "d":
            idx = ask_index(len(report.items), "delete")
            if typer.confirm(f"Delete '{report.items[idx].name}'?", default=True):
                report.items.pop(idx)
        elif action == "q":
            if typer.confirm("Discard this report?", default=False):
                raise typer.Abort()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc=)(["'])(?!https?:|data:|//)(.+?)\2""", re.IGNORECASE)


def inline_local_images(html: str, base_dir: Path) -> str:
    """Embed relative <img> files as data: URIs so the HTML/PDF never depends on file paths."""

    def embed(match: re.Match) -> str:
        path = (base_dir / unquote(match.group(3))).resolve()
        if not path.is_file():
            console.print(f"[yellow]Warning:[/] image not found, left as-is: {escape(str(path))}")
            return match.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        quote = match.group(2)
        return f"{match.group(1)}{quote}data:{mime};base64,{data}{quote}"

    return _IMG_SRC.sub(embed, html)


def template_context(report: Report) -> dict:
    total = float(report.total)
    return {
        "er": {
            "number": report.number,
            "date": report.report_date.strftime(DATE_DISPLAY_FORMAT),
        },
        "items": [
            {
                "name": item.name,
                "description": item.description,
                "quantity": fmt_qty(item.quantity),
                "rate": float(item.rate),
                "amount": float(item.amount),
            }
            for item in report.items
        ],
        "subtotal": total,
        "total": total,
        "total_in_words": amount_in_words(report.total),
    }


def render_html(template_path: Path, report: Report) -> str:
    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        autoescape=select_autoescape(["html"]),  # item names like "R&D <misc>" are escaped safely
        undefined=StrictUndefined,  # a typo like {{ er.numbr }} fails loudly
    )
    try:
        html = env.get_template(template_path.name).render(**template_context(report))
    except TemplateError as exc:
        fail(f"Template problem: {exc}")
    return inline_local_images(html, template_path.parent)


def html_to_pdf(html: str, pdf_path: Path) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail(
            "Playwright isn't installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "or pass --no-pdf to get just the HTML."
        )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="networkidle")  # lets Google Fonts load
                page.evaluate("document.fonts.ready.then(() => true)")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={side: PDF_MARGIN for side in ("top", "right", "bottom", "left")},
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        fail(f"PDF generation failed: {exc}\nIf Chromium is missing, run: playwright install chromium")


def safe_stem(number: str) -> str:
    return re.sub(r"[^\w.-]+", "_", number).strip("._") or "expense_report"


def produce(
    report: Report,
    template: Path,
    out_dir: Path,
    *,
    make_pdf: bool,
    save_json: bool,
    overwrite: Optional[bool],
    on_saved: Optional[Callable[[], None]] = None,
) -> dict:
    """Write HTML (+ JSON, + PDF). `overwrite`: None = ask, True = silently, False = refuse.
    `on_saved` runs once the HTML/JSON are safely on disk (before the slower PDF step)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(report.number)
    paths = {"html": out_dir / f"{stem}.html"}
    if save_json:
        paths["json"] = out_dir / f"{stem}.json"
    if make_pdf:
        paths["pdf"] = out_dir / f"{stem}.pdf"

    clashes = [p.name for p in paths.values() if p.exists()]
    if clashes and overwrite is not True:
        if overwrite is False:
            fail(f"Already exists in {out_dir}: {', '.join(clashes)}")
        typer.confirm(f"{', '.join(clashes)} already in {out_dir}. Overwrite?", abort=True)

    html = render_html(template, report)
    paths["html"].write_text(html, encoding="utf-8")
    if save_json:
        save_report_json(paths["json"], report)
    if on_saved:
        on_saved()
    if make_pdf:
        with console.status("Rendering PDF..."):
            html_to_pdf(html, paths["pdf"])

    return {kind: paths[kind] for kind in ("pdf", "html", "json") if kind in paths}


def print_result(outputs: dict, open_after: bool) -> None:
    console.print("\n[bold green]Report generated[/]")
    for kind, path in outputs.items():
        console.print(f"  {kind.upper():<5} {escape(str(path))}")
    if open_after:
        typer.launch(str(outputs.get("pdf") or outputs["html"]))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
TemplateOpt = Annotated[
    Path,
    typer.Option(
        "--template", "-t", exists=True, dir_okay=False, readable=True,
        help="Jinja2 HTML template.",
    ),
]
PdfOpt = Annotated[
    bool,
    typer.Option("--pdf/--no-pdf", help="Also produce a PDF (needs Playwright + Chromium)."),
]
OpenOpt = Annotated[bool, typer.Option("--open", help="Open the result when finished.")]


@app.command()
def new(
    items_file: Annotated[
        Optional[Path],
        typer.Option(
            "--items-file", "-i", exists=True, dir_okay=False, readable=True,
            help="Preload items from .csv (name, description, quantity, rate) or .json.",
        ),
    ] = None,
    date_text: Annotated[
        Optional[str],
        typer.Option("--date", "-d", help="Report date, e.g. 15-10-2026 (default: today)."),
    ] = None,
    number: Annotated[
        Optional[str],
        typer.Option("--number", "-n", help="Report number (default: next in sequence)."),
    ] = None,
    template: TemplateOpt = DEFAULT_TEMPLATE,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", "-o", file_okay=False, help="Where reports are saved.")
    ] = DEFAULT_OUTPUT_DIR,
    make_pdf: PdfOpt = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="No prompts: use defaults and generate straight away (needs --items-file)."),
    ] = False,
    open_after: OpenOpt = False,
) -> None:
    """Create a new expense report - prompts for the date, number and items."""
    if yes and not items_file:
        fail("--yes needs --items-file (otherwise there are no items to use).")
    console.rule("[bold]New expense report")

    # Date
    if date_text:
        try:
            report_date = parse_date(date_text)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--date") from None
    elif yes:
        report_date = date.today()
    else:
        report_date = typer.prompt(
            "Report date",
            default=date.today().strftime(DATE_DISPLAY_FORMAT),
            value_proc=prompt_proc(parse_date),
        )

    # Number
    suggested = next_report_number(report_date.year)
    if number:
        chosen = number.strip()
    elif yes:
        chosen = suggested
    else:
        chosen = typer.prompt(
            "Report number", default=suggested, value_proc=prompt_proc(require_text)
        )

    # Items
    items = load_items(items_file) if items_file else []
    if items_file:
        console.print(f"Loaded [bold]{len(items)}[/] item(s) from {escape(items_file.name)}")
    report = Report(number=chosen, report_date=report_date, items=items)

    if yes:
        if not items:
            fail(f"{items_file.name} has no items.")
    else:
        if not items:
            console.print("\nEnter your items one at a time. Leave the name blank when you're done.")
            collect_items(items)
        review(report)

    outputs = produce(
        report,
        template,
        output_dir,
        make_pdf=make_pdf,
        save_json=True,
        overwrite=False if yes else None,
        # only advance the counter if the user kept the suggested number
        on_saved=(lambda: commit_report_number(report_date.year))
        if chosen == suggested
        else None,
    )
    print_result(outputs, open_after)


@app.command()
def render(
    report_file: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, readable=True,
            help="A report .json saved by `new` (edit it by hand if you need to fix something).",
        ),
    ],
    template: TemplateOpt = DEFAULT_TEMPLATE,
    output_dir: Annotated[
        Optional[Path],
        typer.Option("--output-dir", "-o", file_okay=False, help="Default: next to the JSON file."),
    ] = None,
    make_pdf: PdfOpt = True,
    open_after: OpenOpt = False,
) -> None:
    """Re-render a saved report (after editing its JSON, or after changing the template)."""
    report = load_report(report_file)
    outputs = produce(
        report,
        template,
        output_dir or report_file.parent,
        make_pdf=make_pdf,
        save_json=False,
        overwrite=True,
    )
    print_result(outputs, open_after)


if __name__ == "__main__":
    app()
