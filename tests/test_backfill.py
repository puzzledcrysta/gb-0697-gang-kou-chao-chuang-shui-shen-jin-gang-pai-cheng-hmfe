from app.pipeline import run
from app import store
from app.modules.m8_backfill.rules import reconcile_receipts, released_occupancy, backfill
from app.errors import BusinessError
import pytest

HEADER="id,kind,start,end,value,aux,version,unit\n"

def tide_rows():
    # 02:00-04:30 潮高3m，吃水9m的船（阈值2m）安全窗约 01:45-05:00，容量180分钟
    out=[]
    for h in range(6):
        for m in (0,30):
            t=f"2026-11-18T{h:02d}:{m:02d}:00+00:00"
            depth=3 if (h==2 or h==3 or (h==4 and m==0) or (h==4 and m==30)) else 1
            out.append(f"T{h}{m//10},tide,{t},,{depth},,1,m")
    return out

def ship_rows(n=6):
    # SHIP01 占60分钟，其余每艘30分钟；6艘共210分钟 > 安全窗180分钟，末艘入候补
    rows=["SHIP01,ship,2026-11-18T00:00:00+00:00,2026-11-18T08:00:00+00:00,9,60,1,m"]
    rows += [f"SHIP{i:02d},ship,2026-11-18T00:00:00+00:00,2026-11-18T08:00:00+00:00,9,30,1,m" for i in range(2,n+1)]
    return rows

def base_csv(extra="",n=6,drafts=None):
    rows=tide_rows()+["P1,pilot,2026-11-18T00:00:00+00:00,2026-11-18T09:00:00+00:00,0,,1,m",
                      "BL1,block,2026-11-18T06:00:00+00:00,2026-11-18T07:00:00+00:00,0,,1,m"]
    if drafts:
        rows += [f"SHIP{i:02d},ship,2026-11-18T00:00:00+00:00,2026-11-18T08:00:00+00:00,{d},30,1,m"
                 for i,d in enumerate(drafts,1)]
    else:
        rows += ship_rows(n)
    return HEADER+"\n".join(rows)+"\n"+extra

def exit_rows(ship="SHIP01",port="02:15:00",pilot="02:15:00",version=1):
    return (f"{ship},exit_port,2026-11-18T{port}+00:00,,0,,{version},m\n"
            f"{ship},exit_pilot,2026-11-18T{pilot}+00:00,,0,,{version},m\n")

def by_id(rows, ident):
    return next(r for r in rows if r["id"]==ident)

def test_reconciled_release_backfills_waitlist():
    r=run(base_csv(exit_rows()))
    release=by_id(r["release_ledger"],"SHIP01")
    assert release["status"]=="APPROVED" and release["mismatch"] is False
    back=by_id(r["backfill_ledger"],"SHIP06")
    assert back["status"]=="BACKFILLED" and back["source_vessel"]=="SHIP01"
    final=by_id(r["final"],"SHIP06")
    assert final["status"]=="BACKFILLED" and final["start_s"]==back["start_s"]
    assert r["blocked"] is False  # 候补变为已排，工作台可查凭据

def test_mismatched_sources_release_at_later_time():
    # 港调报 02:15、引航报 02:30 → 取较晚者 02:30，空档仅15分钟，30分钟船无法递补
    r=run(base_csv(exit_rows(port="02:15:00",pilot="02:30:00")))
    release=by_id(r["release_ledger"],"SHIP01")
    assert release["mismatch"] is True and str(release["release_s"])==str(release["pilot_release_s"])
    back=by_id(r["backfill_ledger"],"SHIP06")
    assert back["status"]=="WAITING" and r["blocked"] is True

def test_single_source_keeps_channel_occupied():
    # 只到港调回执：不能提前腾航道，避免两船撞同一时段
    text=base_csv("SHIP01,exit_port,2026-11-18T02:15:00+00:00,,0,,1,m\n")
    r=run(text)
    release=by_id(r["release_ledger"],"SHIP01")
    assert release["status"]=="PENDING" and release["release_s"] is None
    assert by_id(r["backfill_ledger"],"SHIP06")["status"]=="WAITING"

def test_backfill_still_requires_depth_window():
    # SHIP06 吃水12m（阈值5m），潮高3m不满足；释放再早也不能排
    drafts=[9,9,9,9,9,12]
    r=run(base_csv(exit_rows(),drafts=drafts))
    assert by_id(r["backfill_ledger"],"SHIP06")["status"]=="WAITING"
    assert by_id(r["final"],"SHIP06")["status"]=="NO_SAFE_SLOT"

def test_backfill_still_requires_pilot_window():
    # 引航窗在释放空档之前已关闭：候补不能越过引航约束
    from decimal import Decimal
    ctx={
        "effective":[
            {"id":"A","kind":"ship","a":0,"duration":Decimal(30)},
            {"id":"W","kind":"ship","a":0,"duration":Decimal(30)},
            {"id":"P1","kind":"pilot","a":0,"b":40},
            {"id":"A","kind":"exit_port","a":30,"b":30,"line":9},
            {"id":"A","kind":"exit_pilot","a":30,"b":30,"line":10},
        ],
        "assigned":[{"id":"A","status":"SCHEDULED","start_s":0,"end_s":30,"line":2},
                    {"id":"W","status":"NO_SAFE_SLOT","start_s":None,"end_s":None,"line":3}],
        "channel_windows":[{"id":"W","start_s":0,"end_s":100}],
    }
    releases,backs=backfill(ctx)
    assert releases[0]["status"]=="APPROVED"
    assert backs==[{"id":"W","status":"WAITING","start_s":None,"end_s":None,
                    "release_s":None,"source_vessel":"","line":3}]

def test_stale_receipt_cannot_revoke_approved_release():
    first=base_csv(exit_rows(version=2))
    # 后到的同 ID 低版本记录：按版本机制不生效，已核准释放不被撤回
    late="SHIP01,exit_port,2026-11-18T04:00:00+00:00,,0,,1,m\n"
    r=run(first+late)
    release=by_id(r["release_ledger"],"SHIP01")
    assert release["status"]=="APPROVED"
    assert by_id(r["backfill_ledger"],"SHIP06")["status"]=="BACKFILLED"

def test_retry_does_not_schedule_waitlist_twice():
    text=base_csv(exit_rows())
    once=run(text);twice=run(text)
    assert once["backfill_ledger"]==twice["backfill_ledger"]
    backs=[x for x in twice["backfill_ledger"] if x["status"]=="BACKFILLED"]
    assert len(backs)==len({x["id"] for x in backs})==1
    # 持久化重试：同输入同摘要，复用同一批次
    a=store.save(text,run(text));b=store.save(text,run(text))
    assert a["id"]==b["id"]

def test_duplicate_receipt_same_version_quarantined():
    dup=exit_rows()+"SHIP01,exit_port,2026-11-18T02:15:00+00:00,,0,,1,m\n"
    r=run(base_csv(dup))
    assert any(q["reason"]=="KEY_CONFLICT" and q["id"]=="SHIP01" for q in r["quarantine"])

def test_orphan_receipt_for_unscheduled_ship():
    r=run(base_csv("SHIP99,exit_port,2026-11-18T03:00:00+00:00,,0,,1,m\n"
                   "SHIP99,exit_pilot,2026-11-18T03:00:00+00:00,,0,,1,m\n"))
    assert by_id(r["release_ledger"],"SHIP99")["status"]=="ORPHAN"

def test_release_only_trims_its_own_occupancy():
    releases=[{"id":"A","status":"APPROVED","release_s":50}]
    occupied,gaps=released_occupancy(
        [{"id":"A","status":"SCHEDULED","start_s":0,"end_s":100},
         {"id":"B","status":"SCHEDULED","start_s":100,"end_s":200}],releases)
    assert occupied==[(0,50),(100,200)] and gaps==[(50,100,"A")]

def test_evidence_export_contains_backfill_rows():
    from app.exporter import evidence_csv
    text=base_csv(exit_rows())
    item=store.save(text,run(text))
    csv_text=evidence_csv(item)
    assert "backfill" in csv_text and "BACKFILLED" in csv_text and "SHIP06" in csv_text

def test_module8_api():
    from fastapi.testclient import TestClient
    from app.main import app
    client=TestClient(app,raise_server_exceptions=False)
    reply=client.get("/api/8")
    assert reply.status_code==200 and reply.json()["module"]==8
    page=client.get("/module/8")
    assert page.status_code==200 and "候补递补" in page.text
