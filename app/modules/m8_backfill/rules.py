from app.report import report
from app.modules.m4_channel.rules import subtract
from app.modules.m5_schedule.rules import first_fit
from app.timeops import display

# 港调与引航各自的离道回执；两源到齐才能核准释放
PORT_SOURCE="exit_port"
PILOT_SOURCE="exit_pilot"

def reconcile_receipts(effective, assigned):
    """按船对账双源离道回执。只处理已排船舶；孤儿回执标记 ORPHAN。"""
    receipts={}
    for r in effective:
        if r["kind"] not in (PORT_SOURCE,PILOT_SOURCE):
            continue
        receipts.setdefault(r["id"],{})[r["kind"]]=r
    scheduled={r["id"]:r for r in assigned if r["status"]=="SCHEDULED"}
    releases=[]
    for ship_id,pair in sorted(receipts.items()):
        if ship_id not in scheduled or len(pair)<2:
            releases.append({"id":ship_id,"status":"ORPHAN" if ship_id not in scheduled else "PENDING",
                             "port_release_s":pair.get(PORT_SOURCE,{}).get("a"),
                             "pilot_release_s":pair.get(PILOT_SOURCE,{}).get("a"),
                             "release_s":None,"mismatch":False,
                             "line":next(iter(pair.values()))["line"]})
            continue
        port_s=pair[PORT_SOURCE]["a"];pilot_s=pair[PILOT_SOURCE]["a"]
        # 两源不一致时取较晚者：任一来源都不再占用该航道的最早时刻
        releases.append({"id":ship_id,"status":"APPROVED","port_release_s":port_s,
                         "pilot_release_s":pilot_s,"release_s":max(port_s,pilot_s),
                         "mismatch":port_s!=pilot_s,
                         "line":pair[PORT_SOURCE]["line"]})
    return releases

def released_occupancy(assigned,releases):
    """按核准释放时刻裁剪各船自身占用；释放落在航次外则无新空档。"""
    approved={r["id"]:r["release_s"] for r in releases if r["status"]=="APPROVED"}
    occupied=[];gaps=[]
    for row in assigned:
        if row["status"]!="SCHEDULED":
            continue
        a,b=row["start_s"],row["end_s"];cut=approved.get(row["id"])
        if cut is None or cut>=b:
            occupied.append((a,b))
        elif cut<=a:
            gaps.append((a,b,row["id"]))
        else:
            occupied.append((a,cut));gaps.append((cut,b,row["id"]))
    return occupied,gaps

def backfill(ctx):
    releases=reconcile_receipts(ctx["effective"],ctx["assigned"])
    occupied,gaps=released_occupancy(ctx["assigned"],releases)
    pilots=[(r["a"],r["b"]) for r in ctx["effective"] if r["kind"]=="pilot"]
    ships={r["id"]:r for r in ctx["effective"] if r["kind"]=="ship"}
    waitlist=sorted((r for r in ctx["assigned"] if r["status"]!="SCHEDULED"),
                    key=lambda r:(ships[r["id"]]["a"],r["id"]))
    backfills=[]
    for candidate in waitlist:
        ship=ships[candidate["id"]]
        windows=[(w["start_s"],w["end_s"]) for w in ctx["channel_windows"] if w["id"]==candidate["id"]]
        hit=first_fit(windows,pilots,ship["duration"],occupied)
        if hit is None:
            backfills.append({"id":candidate["id"],"status":"WAITING","start_s":None,"end_s":None,
                              "release_s":None,"source_vessel":"","line":candidate["line"]})
            continue
        occupied.append(hit)
        source=next((sid for lo,hi,sid in gaps if lo<=hit[0] and hit[1]<=hi),"")
        row={"id":candidate["id"],"status":"BACKFILLED","start_s":hit[0],"end_s":hit[1],
             "release_s":hit[0],"source_vessel":source,"line":candidate["line"]}
        backfills.append(row)
    return releases,backfills

def apply(ctx):
    releases,backfills=backfill(ctx)
    ctx["release_ledger"]=releases;ctx["backfill_ledger"]=backfills
    promoted={r["id"]:r for r in backfills if r["status"]=="BACKFILLED"}
    final=[]
    for row in ctx["final"]:
        x=dict(row)
        if row["id"] in promoted:
            p=promoted[row["id"]]
            x.update(status="BACKFILLED",start_s=p["start_s"],end_s=p["end_s"],
                     start=display(p["start_s"]),end=display(p["end_s"]),
                     source_vessel=p["source_vessel"])
        final.append(x)
    ctx["final"]=final
    ctx["blocked"]=bool(ctx["conflicts"]) or any(r["status"] not in ("SCHEDULED","BACKFILLED") for r in final)
    rows=[{"ledger":"release",**r} for r in releases]+[{"ledger":"backfill",**r} for r in backfills]
    return report(rows,
                  核准释放=sum(r["status"]=="APPROVED" for r in releases),
                  待对账=sum(r["status"]=="PENDING" for r in releases),
                  孤儿回执=sum(r["status"]=="ORPHAN" for r in releases),
                  递补已排=sum(r["status"]=="BACKFILLED" for r in backfills),
                  仍候补=sum(r["status"]=="WAITING" for r in backfills),
                  双源不一致=sum(1 for r in releases if r.get("mismatch")))
