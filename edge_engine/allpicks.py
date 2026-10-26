"""Every pick on one searchable page (picks.html): player props, team bets and the model's
suggested entries, filterable by date, team, sport and result. The track record page links here
instead of listing everything itself."""
import json
from datetime import datetime

from . import RETIRED_MODELS
from .ui import esc, page

TABS = (("props", "Props"), ("teams", "Team bets"), ("entries", "Suggested entries"))


def _dt(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _day(iso, tz):
    t = _dt(iso)
    return t.astimezone(tz).strftime("%Y-%m-%d") if t else ""


def _time(iso, tz):
    t = _dt(iso)
    return t.astimezone(tz).strftime("%I:%M %p").lstrip("0") if t else ""


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def data(conn, tz):
    """Compact rows for the page. Keys are short because the whole record is embedded in it."""
    from .teams import MARKET_NAME, ensure
    picks = {}
    props = []
    for r in conn.execute("SELECT id, model_version, sport, app, player, stat, line, side, tier, home, away, start, "
                          "fair, edge, clv, result, actual FROM picks ORDER BY start DESC, seq DESC"):
        p = {"d": _day(r["start"], tz), "t": _time(r["start"], tz), "sp": (r["sport"] or "").upper(), "ap": r["app"] or "",
             "pl": r["player"], "st": r["stat"], "ln": r["line"], "sd": r["side"], "ti": r["tier"] or "",
             "h": r["home"] or "", "a": r["away"] or "", "f": _r(r["fair"]), "e": _r(r["edge"]), "c": _r(r["clv"]),
             "r": r["result"] or "", "x": r["actual"]}
        if r["model_version"] in RETIRED_MODELS:
            p["old"] = r["model_version"]
        picks[r["id"]] = p
        props.append(p)

    ensure(conn)
    teams = []
    for r in conn.execute("SELECT * FROM team_plays ORDER BY start DESC"):
        pt = r["point"]
        tail = "" if pt is None or r["market"] == "h2h" else f" {pt:+g}" if r["market"] == "spreads" else f" {pt:g}"
        teams.append({"d": _day(r["start"], tz), "t": _time(r["start"], tz), "sp": (r["sport"] or "").upper(),
                      "h": r["home"] or "", "a": r["away"] or "", "m": MARKET_NAME.get(r["market"], r["market"]),
                      "o": f'{r["outcome"]}{tail}', "bk": r["book"] or "", "od": r["odds"], "f": _r(r["fair"]),
                      "ev": _r(r["ev"]), "r": r["result"] or "", "hs": r["home_score"], "as": r["away_score"]})

    def legs_of(ids):
        return [picks[i] for i in json.loads(ids or "[]") if i in picks]

    entries = []
    for r in conn.execute("SELECT * FROM flex_slips ORDER BY day DESC, n, seq"):
        legs = legs_of(r["pick_ids"])
        entries.append({"d": r["day"], "k": "Daily Flex slip", "en": r["entry"], "ap": r["app"] or "", "s": r["status"],
                        "ret": _r(r["ret"]), "hits": r["hits"], "ev": _r(r["ev"]), "cash": _r(r["cash"]), "legs": legs})
    for r in conn.execute("SELECT * FROM system_entries ORDER BY seq DESC"):
        legs = legs_of(r["pick_ids"])
        starts = [l["d"] for l in legs if l["d"]]
        entries.append({"d": min(starts) if starts else _day(r["created_at"], tz), "k": "Model portfolio",
                        "en": r["entry"], "ap": r["app"] or "", "s": r["status"], "ret": _r(r["ret"]),
                        "hits": sum(l["r"] == "hit" for l in legs) if r["status"] == "settled" else None,
                        "ev": _r(r["ev"]), "sk": _r(r["stake_pct"], 5), "legs": legs})
    entries.sort(key=lambda e: e["d"], reverse=True)
    return {"props": props, "teams": teams, "entries": entries}


CSS = """
.ftabs{display:flex;gap:6px;flex-wrap:wrap}
.ftabs button,.fres button{appearance:none;font:600 .88rem var(--body);border:1px solid var(--line);background:var(--panel2);color:var(--muted);
border-radius:99px;padding:8px 14px;min-height:40px;cursor:pointer}
.ftabs button[aria-pressed="true"],.fres button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.fbar{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:14px}
.fbar select{width:100%;min-width:0;padding:8px 6px}
.fres{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;align-items:center}
.fcount{margin-left:auto}
.pday{font:700 1.1rem/1.2 var(--display);margin:18px 0 2px;padding-top:10px;border-top:1px solid var(--line)}
.pday:first-child{border-top:0;margin-top:0;padding-top:0}
.plist{list-style:none;margin:0;padding:0}
.pick{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;padding:10px 0;border-bottom:1px solid var(--line)}
.pick:last-child{border-bottom:0}
.pick .who{font-weight:600;overflow-wrap:anywhere}.pick .what{font-size:.88rem}.pick .meta{font-size:.78rem;color:var(--muted)}
.pick .right{grid-column:2;grid-row:1 / span 3;text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.pick .big{font:500 .95rem var(--mono)}
.ents{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:12px;margin-top:6px}
.ent{border:1px solid var(--line);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px}
.ent .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ent ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px;font-size:.88rem}
.mk{font-weight:700;margin-left:4px}
.more{margin-top:14px}
"""

JS = r"""
(function(){
var D=JSON.parse(document.getElementById('allData').textContent);
var $=function(s){return document.querySelector(s)};
var E=function(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
var pct=function(x,d){return x==null?'–':(x*100).toFixed(d==null?1:d)+'%'};
var pts=function(x){if(x==null)return'–';var v=Math.round(x*1000)/10;return v===0?'0.0':(v>0?'+':'−')+Math.abs(v).toFixed(1)};
var cl=function(x){return x==null||Math.abs(x)<0.0005?'':x>0?'pos':'neg'};
var num=function(x){return x==null?'':(+x).toString()};
var S={tab:'props',sport:'',team:'',date:'',res:'all',limit:150};
try{var h=location.hash.slice(1);if(['props','teams','entries'].indexOf(h)>=0)S.tab=h;}catch(e){}
function teamsOf(r){return r.legs?r.legs.reduce(function(a,l){return a.concat([l.h,l.a])},[]):[r.h,r.a]}
function sportsOf(r){return r.legs?r.legs.map(function(l){return l.sp}):[r.sp]}
function isOpen(r){return r.legs?r.s==='open':!r.r}
function match(r){
  if(S.date&&r.d!==S.date)return false;
  if(S.sport&&sportsOf(r).indexOf(S.sport)<0)return false;
  if(S.team&&teamsOf(r).indexOf(S.team)<0)return false;
  if(S.res==='open'&&!isOpen(r))return false;
  if(S.res==='graded'&&isOpen(r))return false;
  return true;}
function fillSelects(){
  var all=D.props.concat(D.teams).concat(D.entries.reduce(function(a,e){return a.concat(e.legs)},[]));
  var sports={},teams={},days={};
  all.forEach(function(r){if(r.sp)sports[r.sp]=1;if(r.d)days[r.d]=1;
    if(!S.sport||r.sp===S.sport){if(r.h)teams[r.h]=1;if(r.a)teams[r.a]=1;}});
  D.entries.forEach(function(e){if(e.d)days[e.d]=1});
  var opt=function(v,t,sel){return'<option value="'+E(v)+'"'+(v===sel?' selected':'')+'>'+E(t)+'</option>'};
  $('#fSport').innerHTML=opt('','All sports',S.sport)+Object.keys(sports).sort().map(function(s){return opt(s,s,S.sport)}).join('');
  if(S.team&&!teams[S.team])S.team='';
  $('#fTeam').innerHTML=opt('','All teams',S.team)+Object.keys(teams).sort().map(function(s){return opt(s,s,S.team)}).join('');
  $('#fDate').innerHTML=opt('','All dates',S.date)+Object.keys(days).sort().reverse().map(function(d){return opt(d,label(d),S.date)}).join('');}
function label(d){var p=d.split('-');return new Date(+p[0],p[1]-1,+p[2]).toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric',year:'numeric'})}
var CHIP={hit:['good','Hit'],miss:['bad','Miss'],win:['good','Win'],loss:['bad','Loss'],push:['warn','Push'],void:['warn','Void'],unmatched:['warn','No stat'],ungraded:['warn','Ungraded']};
function chip(r,x){if(!r)return'<span class="muted small">Pending</span>';var c=CHIP[r]||['warn',r];
  return'<span class="chip '+c[0]+'">'+E(c[1])+(x!=null&&x!==''?' · '+E(x):'')+'</span>'}
function game(r){return r.h||r.a?E(r.a)+' @ '+E(r.h):''}
function propRow(r){
  var meta=[E(r.sp),E(r.ap)];if(r.ti)meta.push(E(r.ti));if(r.old)meta.push('<span class="warn">retired model '+E(r.old)+'</span>');
  if(game(r))meta.push(game(r));if(r.t)meta.push(E(r.t));
  return'<li class="pick"><span class="who">'+E(r.pl)+'</span><span class="what"><span class="side '+E(r.sd)+'">'+E(r.sd)+'</span> '+num(r.ln)+' '+E(r.st)+'</span>'
   +'<span class="meta">'+meta.join(' · ')+'</span><span class="right"><span class="big">'+pct(r.f)+'</span>'
   +'<span class="small '+cl(r.e)+'">edge '+pts(r.e)+(r.c!=null?' · CLV <span class="'+cl(r.c)+'">'+pts(r.c)+'</span>':'')+'</span>'+chip(r.r,r.x)+'</span></li>'}
function teamRow(r){
  var odds=r.od==null?'–':(r.od>0?'+':'')+r.od, score=r.hs!=null?' '+num(r.as)+'-'+num(r.hs):'';
  return'<li class="pick"><span class="who">'+E(r.m)+': '+E(r.o)+'</span><span class="what">'+game(r)+'</span>'
   +'<span class="meta">'+E(r.sp)+(r.t?' · '+E(r.t):'')+' · '+E(r.bk)+' '+odds+'</span>'
   +'<span class="right"><span class="big '+cl(r.ev)+'">'+(r.ev==null?'–':(r.ev>0?'+':'')+pct(r.ev))+' EV</span>'
   +'<span class="small muted">fair '+pct(r.f)+'</span>'+chip(r.r, score?score.trim():null)+'</span></li>'}
var MARK={hit:['pos','✓'],miss:['neg','✗'],push:['muted','–'],void:['muted','–']};
function entCard(e){
  var c=e.s==='open'?'<span class="chip warn">Open</span>':e.s==='void'?'<span class="chip warn">Void · refunded</span>'
    :'<span class="chip '+(e.ret>=1?'good':'bad')+'">'+(e.hits!=null?e.hits+'/'+e.legs.length+' · ':'')+num(e.ret)+'x</span>';
  var lis=e.legs.map(function(l){var m=MARK[l.r]||['muted',''];
    return'<li><span class="side '+E(l.sd)+'">'+E(l.sd)+'</span> '+E(l.pl)+' <span class="muted">'+num(l.ln)+' '+E(l.st)+'</span>'
     +(l.x!=null&&l.x!==''?' <span class="muted small">('+E(l.x)+')</span>':'')+'<span class="mk '+m[0]+'">'+m[1]+'</span></li>'}).join('');
  var foot='Expected return <span class="'+cl(e.ev-1)+'">'+(e.ev>1?'+':'')+pct(e.ev-1)+'</span>'
    +(e.sk!=null?' · stake '+pct(e.sk,2)+' of bankroll':'')+(e.cash!=null?' · stake back '+pct(e.cash,0)+' of the time':'');
  return'<div class="ent"><div class="top"><b>'+E(e.en)+'</b>'+c+'</div><span class="muted small">'+E(e.k)+' · '+E(e.ap)+'</span>'
   +'<ol>'+lis+'</ol><span class="muted small">'+foot+'</span></div>'}
function render(){
  document.querySelectorAll('.ftabs button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.t===S.tab?'true':'false')});
  document.querySelectorAll('.fres button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.r===S.res?'true':'false')});
  var rows=D[S.tab].filter(match), shown=rows.slice(0,S.limit), byDay={}, order=[];
  shown.forEach(function(r){var d=r.d||'';if(!byDay[d]){byDay[d]=[];order.push(d)}byDay[d].push(r)});
  var fn=S.tab==='props'?propRow:S.tab==='teams'?teamRow:entCard, wrapO=S.tab==='entries'?'<div class="ents">':'<ul class="plist">', wrapC=S.tab==='entries'?'</div>':'</ul>';
  var noun=S.tab==='props'?'pick':S.tab==='teams'?'bet':'entry';
  var pl=function(n){return n+' '+(n===1?noun:noun==='entry'?'entries':noun+'s')};
  $('#fCount').textContent=pl(rows.length);
  $('#out').innerHTML=rows.length?order.map(function(d){return'<h3 class="pday">'+(d?E(label(d)):'Date unknown')+' <span class="muted small">· '+pl(byDay[d].length)+'</span></h3>'+wrapO+byDay[d].map(fn).join('')+wrapC}).join('')
    +(rows.length>shown.length?'<button class="btn more" id="more">Show more ('+(rows.length-shown.length).toLocaleString()+' left)</button>':'')
    :'<p class="muted">Nothing matches these filters.</p>';
  var m=$('#more');if(m)m.onclick=function(){S.limit+=300;render()};}
document.querySelectorAll('.ftabs button').forEach(function(b){b.onclick=function(){S.tab=b.dataset.t;S.limit=150;try{history.replaceState(null,'','#'+S.tab)}catch(e){}render()}});
document.querySelectorAll('.fres button').forEach(function(b){b.onclick=function(){S.res=b.dataset.r;S.limit=150;render()}});
$('#fSport').onchange=function(){S.sport=this.value;S.limit=150;fillSelects();render()};
$('#fTeam').onchange=function(){S.team=this.value;S.limit=150;render()};
$('#fDate').onchange=function(){S.date=this.value;S.limit=150;render()};
$('#fClear').onclick=function(){S.sport=S.team=S.date='';S.res='all';S.limit=150;fillSelects();render()};
addEventListener('hashchange',function(){var h=location.hash.slice(1);if(D[h]&&h!==S.tab){S.tab=h;S.limit=150;render()}});
fillSelects();render();
})();
"""


def html(conn, nav=None, tz=None):
    from zoneinfo import ZoneInfo
    tz = tz or ZoneInfo("America/Chicago")
    d = data(conn, tz)
    blob = json.dumps(d, separators=(",", ":"), default=str).replace("</", "<\\/")
    counts = {k: len(d[k]) for k, _ in TABS}
    tabs = "".join(f'<button type="button" data-t="{k}" aria-pressed="{"true" if k == "props" else "false"}">'
                   f'{esc(t)} <span class="num">{counts[k]:,}</span></button>' for k, t in TABS)
    zone = "Central" if str(tz) == "America/Chicago" else esc(str(tz))
    body = f"""<style>{CSS}</style>
<div><h1>All picks</h1><p class="lead">Every player prop, team bet and suggested entry the model has logged, wins and losses
alike. Fair % is the model's chance a pick hits; edge is how far that sits above break-even. Times are {zone} time.
<a href="results.html">Back to the track record</a></p></div>
<div class="panel">
<div class="ftabs" role="group" aria-label="What to show">{tabs}</div>
<div class="fbar">
<label>Date<select id="fDate"></select></label>
<label>Sport<select id="fSport"></select></label>
<label>Team<select id="fTeam"></select></label>
</div>
<div class="fres" role="group" aria-label="Result">
<button type="button" data-r="all" aria-pressed="true">All</button><button type="button" data-r="open" aria-pressed="false">Open</button>
<button type="button" data-r="graded" aria-pressed="false">Graded</button>
<button type="button" class="btn" id="fClear" style="min-height:40px;padding:6px 12px">Clear filters</button>
<span class="fcount muted small" id="fCount" aria-live="polite"></span></div>
</div>
<div class="panel" id="out"><p class="muted">Loading…</p></div>
<noscript><p class="muted">This page needs JavaScript. The full record is also in <a href="picks.csv">picks.csv</a>.</p></noscript>
<script type="application/json" id="allData">{blob}</script>
<script>{JS}</script>"""
    return page("Edge Finder · All picks", body, nav, "/picks",
                action='<a class="btn" href="results.html">Track record</a> <a class="btn primary" href="app.html">Open the app</a>')
