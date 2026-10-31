"""Run explicitly: python -m app.modules.analytics_bot. No FastAPI side effects."""
import asyncio
import fcntl
import logging
from datetime import datetime, timezone

import httpx

from .assistant import Assistant
from .config import AnalyticsBotSettings
from .reports import PostgresSales, UnconnectedAds
from .service import Analyst, Telegram
from .state import State

log = logging.getLogger("analytics_bot")


async def run(cfg):
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    # One poller per persistent volume; a second instance fails before polling.
    with open(str(cfg.state_path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = State(cfg.state_path)
        assistant = None
        try:
            async with httpx.AsyncClient() as client:
                tg = Telegram(cfg, client)
                me = await tg.call("getMe", {})
                sales, ads = PostgresSales(cfg.sales_database_url.get_secret_value()), UnconnectedAds()
                assistant = Assistant(cfg, sales, ads, state)
                analyst = Analyst(cfg, tg, state, sales, ads, me["username"], me["id"], assistant)
                log.info("Analytics bot started; daily=%s", cfg.daily_enabled)
                while True:
                    try:
                        await analyst.daily(datetime.now(timezone.utc))
                    except Exception:
                        log.warning("Daily report not delivered; will retry")
                    try:
                        updates = await tg.call("getUpdates", {"offset": int(state.get("offset", "0")),
                                                               "timeout": 20, "allowed_updates": ["message"]})
                        for update in updates:
                            if update["update_id"] < int(state.get("offset", "0")):
                                continue
                            await analyst.handle(update, datetime.now(timezone.utc))
                            state.set("offset", update["update_id"] + 1)
                    except Exception:
                        log.warning("Polling or command delivery failed; will retry")
                        await asyncio.sleep(15)
        finally:
            if assistant is not None:
                await assistant.close()
            state.close()


def main():
    logging.basicConfig(level=logging.WARNING)
    log.setLevel(logging.INFO)
    try:
        cfg = AnalyticsBotSettings()
    except Exception:
        # Pydantic's raw validation errors may contain entered secrets.
        raise SystemExit("Invalid ANALYTICS_BOT configuration; check the setup guide") from None
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Analytics bot stopped; check configuration, network and instance lock") from None


if __name__ == "__main__":
    main()
