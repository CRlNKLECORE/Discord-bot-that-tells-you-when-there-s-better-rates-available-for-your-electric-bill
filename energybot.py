# bot.py
import re
import os
import json
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import time as dtime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import discord
from discord.ext import commands, tasks

# ----------------------------
# Config
# ----------------------------
API_URL = (
    "https://www.energizect.com/ectr_search_api/offers"
    "?customerClass[]=INSERTCUSTOMERCLASS&monthlyUsage=INSERTUSAGE&planTypeEdc[]=INSERTPLANTYPE&"
)
DATA_FILE = "rates.json"
CHECK_TIME_ET = dtime(hour=10, minute=0, tzinfo=ZoneInfo("America/New_York"))  # daily 10:00 AM ET

RATE_INPUT_RE = re.compile(r"^0\.(\d+)$")  # must start with 0. and then digits
Q5 = Decimal("0.00001")  # 5 digits after decimal


# ----------------------------
# Persistence helpers
# ----------------------------
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, DATA_FILE)


# ----------------------------
# Rate parsing / validation
# ----------------------------
class RateParseError(ValueError):
    pass


def parse_user_rate(raw: str) -> Decimal:
    """
    Requirements:
      - Must look like 0.xxxxx (starts with 0.)
      - No negatives
      - Must have AT LEAST 5 digits after decimal in the input
      - Value must be 0 <= rate < 1
      - Stored rate is rounded to exactly 5 digits
    """
    s = raw.strip()

    if s.startswith("-"):
        raise RateParseError("Rate cannot be negative.")

    m = RATE_INPUT_RE.match(s)
    if not m:
        raise RateParseError("Formatting should be 0.xxxxx (must start with 0. and use digits).")

    frac = m.group(1)
    if len(frac) < 5:
        raise RateParseError("Formatting should be 0.xxxxx (needs at least 5 digits after the decimal).")

    try:
        d = Decimal(s)
    except InvalidOperation:
        raise RateParseError("That rate isn't a valid decimal number.")

    if d < 0:
        raise RateParseError("Rate cannot be negative.")
    if d >= 1:
        raise RateParseError("Rate must be less than 1.00000 (prices should start with 0.xxxxx).")

    # round to 5 digits after decimal
    return d.quantize(Q5, rounding=ROUND_HALF_UP)


def parse_offer_rate(obj: dict) -> Decimal | None:
    """
    Offer JSON sometimes provides:
      - 'rate' as a string like "0.12290"
      - 'blendedRate' as a float like 0.1229
    We'll prefer 'rate' if present.
    """
    val = obj.get("rate")
    if val is None:
        val = obj.get("blendedRate")
    if val is None:
        return None

    try:
        d = Decimal(str(val))
    except InvalidOperation:
        return None

    if d < 0 or d >= 1:
        return None

    return d.quantize(Q5, rounding=ROUND_HALF_UP)


# ----------------------------
# Discord bot setup
# ----------------------------
intents = discord.Intents.default()
intents.message_content = True  # REQUIRED for prefix commands in discord.py 2.x

bot = commands.Bot(command_prefix="!", intents=intents)

# Global browser instance (initialized on first use)
_browser: Browser | None = None
_browser_context: BrowserContext | None = None


async def get_browser() -> tuple[Browser, BrowserContext]:
    """Get or create a browser instance for headless browsing."""
    global _browser, _browser_context
    
    if _browser is None or not _browser.is_connected():
        playwright = await async_playwright().start()
        _browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        _browser_context = await _browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/139.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
    
    return _browser, _browser_context


async def fetch_offers() -> list[dict]:
    browser, context = await get_browser()
    page: Page = await context.new_page()
    
    try:
        # Set additional headers
        await page.set_extra_http_headers({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.energizect.com/",
        })
        
        # Navigate to the API URL
        response = await page.goto(API_URL, wait_until="networkidle", timeout=30000)
        
        if response is None:
            raise RuntimeError("Failed to get response from EnergizeCT API")
        
        if response.status == 403:
            raise RuntimeError(
                "403 Forbidden from EnergizeCT (likely Cloudflare protection). "
                "Browser request was blocked."
            )
        
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}: {response.status_text}")
        
        # Get the JSON response
        data = await response.json()
        
    finally:
        await page.close()

    offers_raw = (data.get("results") or []) + (data.get("compareResults") or [])
    offers: list[dict] = []

    for o in offers_raw:
        rdec = parse_offer_rate(o)
        if rdec is None:
            continue

        content_url = o.get("contentUrl") or ""
        energizect_url = ("https://www.energizect.com" + content_url) if isinstance(content_url, str) and content_url.startswith("/") else None
        offer_link = o.get("offerLink", {}).get("uri") if isinstance(o.get("offerLink"), dict) else None

        offers.append({
            "id": str(o.get("id", "")),
            "supplier": o.get("supplier") or o.get("title") or "Unknown Supplier",
            "title": o.get("title") or o.get("supplier") or "Offer",
            "offerType": o.get("offerType") or "Unknown",
            "termOfOffer": o.get("termOfOffer") or "Unknown term",
            "fees": o.get("fees") or [],
            "recLabel": o.get("recLabel"),
            "standardOffer": bool(o.get("standardOffer", False)),
            "rate_dec": rdec,
            "rate_str": f"{rdec:.5f}",
            "energizect_url": energizect_url,
            "enroll_url": offer_link,
        })

    offers.sort(key=lambda x: x["rate_dec"])
    return offers


def money_savings_per_month(user_rate: Decimal, best_rate: Decimal, monthly_kwh: int = 750) -> Decimal:
    diff = (user_rate - best_rate)
    if diff <= 0:
        return Decimal("0.00")
    return (diff * Decimal(monthly_kwh)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def send_notification(user_id: int, info: dict, text: str) -> None:
    """
    Try to send in the saved channel with a mention; fallback to DM.
    """
    channel_id = info.get("notify_channel_id")
    channel = bot.get_channel(channel_id) if channel_id else None

    # Prefer channel mention if possible
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(f"<@{user_id}> {text}")
            return
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    # Fallback: DM
    try:
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except discord.Forbidden:
        # Can't DM the user (privacy settings)
        pass


def format_offer_block(o: dict) -> str:
    fees = ", ".join(o["fees"]) if o["fees"] else "N/A"
    links = []
    if o.get("energizect_url"):
        links.append(f"EnergizeCT: {o['energizect_url']}")
    if o.get("enroll_url"):
        links.append(f"Enroll: {o['enroll_url']}")
    link_text = "\n".join(links) if links else "Link: N/A"

    rec = f" • {o['recLabel']}" if o.get("recLabel") else ""
    std = " (Standard Offer)" if o.get("standardOffer") else ""

    return (
        f"**{o['supplier']}**{std}\n"
        f"- Rate: **{o['rate_str']}** $/kWh\n"
        f"- Type: {o['offerType']}\n"
        f"- Term: {o['termOfOffer']}{rec}\n"
        f"- Fees: {fees}\n"
        f"{link_text}"
    )


# ----------------------------
# Commands
# ----------------------------
@bot.command(name="setrate")
async def setrate(ctx: commands.Context, rate: str):
    """
    Usage: !setrate 0.12641
    Stores per-user rate and remembers the channel to ping in.
    """
    try:
        d = parse_user_rate(rate)
    except RateParseError as e:
        await ctx.reply(f"❌ {e}")
        return

    data = load_data()
    uid = str(ctx.author.id)

    # store per user
    data.setdefault(uid, {})
    data[uid]["rate"] = f"{d:.5f}"
    data[uid]["notify_channel_id"] = ctx.channel.id
    data[uid]["notify_guild_id"] = ctx.guild.id if ctx.guild else None

    # reset notification state so you can get notified again on next check if better exists
    data[uid].setdefault("last_notified_offer_id", None)
    data[uid].setdefault("last_notified_rate", None)

    save_data(data)

    await ctx.reply(
        f"✅ Saved your current rate as **{d:.5f}** $/kWh.\n"
        f"I’ll check EnergizeCT daily at **{CHECK_TIME_ET.hour:02d}:{CHECK_TIME_ET.minute:02d} ET** and ping you if a cheaper offer is available."
    )


@bot.command(name="rate")
async def showrate(ctx: commands.Context):
    """Show your stored rate."""
    data = load_data()
    uid = str(ctx.author.id)
    if uid not in data or "rate" not in data[uid]:
        await ctx.reply("You haven’t set a rate yet. Use `!setrate 0.12641`.")
        return
    await ctx.reply(f"Your stored rate is **{data[uid]['rate']}** $/kWh.")


@bot.command(name="checknow")
async def checknow(ctx: commands.Context):
    """Manually run a check and show the best current offer."""
    data = load_data()
    uid = str(ctx.author.id)
    if uid not in data or "rate" not in data[uid]:
        await ctx.reply("You haven’t set a rate yet. Use `!setrate 0.12641`.")
        return

    user_rate = Decimal(data[uid]["rate"])
    try:
        offers = await fetch_offers()
    except Exception as e:
        await ctx.reply(f"❌ API check failed: `{type(e).__name__}: {e}`")
        return

    if not offers:
        await ctx.reply("No offers returned from the API.")
        return

    better = [o for o in offers if o["rate_dec"] < user_rate]
    best = offers[0]

    msg = f"Best current offer:\n{format_offer_block(best)}"
    if better:
        msg += f"\n\n✅ Cheaper than your rate (**{user_rate:.5f}**): **{better[0]['rate_str']}**"
    else:
        msg += f"\n\nNo offer is cheaper than your rate (**{user_rate:.5f}**)."

    await ctx.reply(msg)


# ----------------------------
# Daily task
# ----------------------------
@tasks.loop(time=CHECK_TIME_ET)
async def daily_check():
    data = load_data()
    if not data:
        return

    try:
        offers = await fetch_offers()
    except Exception:
        return

    if not offers:
        return

    # Precompute best offers (sorted)
    for uid_str, info in data.items():
        if "rate" not in info:
            continue

        try:
            user_rate = Decimal(info["rate"])
        except InvalidOperation:
            continue

        better = [o for o in offers if o["rate_dec"] < user_rate]
        if not better:
            continue

        best = better[0]
        last_id = info.get("last_notified_offer_id")
        last_rate = info.get("last_notified_rate")

        # Don’t spam the same best offer repeatedly
        if last_id == best["id"] and last_rate == best["rate_str"]:
            continue

        savings = money_savings_per_month(user_rate, best["rate_dec"], monthly_kwh=750)
        top3 = better[:3]

        lines = [
            f"⚡ **Cheaper electricity rate found**",
            f"Your rate: **{user_rate:.5f}** $/kWh",
            f"Best offer: **{best['rate_str']}** $/kWh (save ~**${savings}** / month @ 750 kWh)",
            "",
            format_offer_block(best),
        ]

        if len(top3) > 1:
            lines.append("\nOther cheaper options:")
            for o in top3[1:]:
                lines.append(f"- {o['supplier']}: {o['rate_str']} $/kWh • {o['termOfOffer']}")

        text = "\n".join(lines)

        await send_notification(int(uid_str), info, text)

        # Update notification state
        info["last_notified_offer_id"] = best["id"]
        info["last_notified_rate"] = best["rate_str"]

    save_data(data)


@daily_check.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    if not daily_check.is_running():
        daily_check.start()
    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_disconnect():
    """Clean up browser resources on disconnect."""
    global _browser, _browser_context
    if _browser_context:
        await _browser_context.close()
        _browser_context = None
    if _browser:
        await _browser.close()
        _browser = None


# ----------------------------
# Entrypoint
# ----------------------------
def main():
    token = "INSERT_TOKEN_HERE"
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN environment variable.")
    bot.run(token)


if __name__ == "__main__":
    main()
