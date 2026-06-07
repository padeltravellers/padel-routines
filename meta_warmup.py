#!/usr/bin/env python3
"""
meta_warmup.py — calls read-only exitosos a Meta Marketing API para
acumular historial de uso y bajar error rate antes de re-aplicar a App Review.

Ejecutar vía cron 3-4 veces al día durante 15 días.
"""
import os, sys, json, time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.ad import Ad

FacebookAdsApi.init(access_token=os.environ["META_ACCESS_TOKEN"])
ACC = AdAccount(os.environ["META_AD_ACCOUNT_ID"])

LOG = Path(__file__).parent / "output" / "meta_warmup.log"
LOG.parent.mkdir(exist_ok=True)


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def run():
    ok, err = 0, 0
    calls = [
        # 1) Account info
        lambda: ACC.api_get(fields=["name","currency","account_status","amount_spent","balance"]),
        # 2) Campaigns (listado)
        lambda: list(ACC.get_campaigns(fields=["id","name","status","objective"])),
        # 3) AdSets
        lambda: list(ACC.get_ad_sets(fields=["id","name","status","daily_budget"])),
        # 4) Ads
        lambda: list(ACC.get_ads(fields=["id","name","effective_status"])),
        # 5) Insights campaign level last_7d
        lambda: list(ACC.get_insights(params={"level":"campaign","date_preset":"last_7d",
                                              "fields":["campaign_name","spend","impressions","clicks","ctr","cpm"]})),
        # 6) Insights adset level last_7d
        lambda: list(ACC.get_insights(params={"level":"adset","date_preset":"last_7d",
                                              "fields":["adset_name","spend","clicks"]})),
        # 7) Insights ad level last_7d
        lambda: list(ACC.get_insights(params={"level":"ad","date_preset":"last_7d",
                                              "fields":["ad_name","spend","impressions"]})),
        # 8) Custom audiences
        lambda: list(ACC.get_custom_audiences(fields=["id","name","subtype"])),
        # 9) Ad videos
        lambda: list(ACC.get_ad_videos(fields=["id","title"])),
        # 10) Ad creatives
        lambda: list(ACC.get_ad_creatives(fields=["id","name"])),
    ]
    for i, call in enumerate(calls, 1):
        try:
            result = call()
            n = len(result) if isinstance(result, list) else 1
            ok += 1
            log(f"  ✓ call {i}: {n} items")
        except Exception as e:
            err += 1
            log(f"  ✗ call {i}: {str(e)[:150]}")
        time.sleep(0.5)
    log(f"RUN DONE: {ok} OK / {err} err")
    print(f"{ok} OK, {err} err")
    return ok, err


if __name__ == "__main__":
    log(f"=== Warmup run ===")
    run()
