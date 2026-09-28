# -*- coding: utf-8 -*-
"""
飞书开放平台客户端(仅标准库,无需 pip install)
功能:tenant_access_token 获取/缓存、会议室忙闲查询、会议室日程查询

注意:freebusy / events 的请求体字段名以飞书 API 调试台的实际返回为准,
     联调时如果报字段错误,优先核对 _TUNABLE 标记的两处。
"""
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE = "https://open.feishu.cn"
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
OAUTH_TOKEN_URL = BASE + "/open-apis/authen/v2/oauth/token"


class FeishuError(Exception):
    pass


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._token_expire = 0.0

    # ---------- 底层 HTTP ----------
    def _http(self, method: str, path: str, body=None, params=None, auth=True) -> dict:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if auth:
            req.add_header("Authorization", "Bearer " + self.token())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 飞书在 4xx 时也会返回 JSON 错误体,读出来才知道具体原因
            detail = e.read().decode("utf-8", "replace")
            raise FeishuError(f"HTTP {e.code} ({method} {path}): {detail}") from e
        if payload.get("code") != 0:
            raise FeishuError(f"[{payload.get('code')}] {payload.get('msg')}  ({method} {path})")
        return payload

    # ---------- 凭证 ----------
    def token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        data = self._http(
            "POST", "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        return self._token

    # ---------- 会议室忙闲(只有时间段,没有预约人) ----------
    def freebusy(self, room_id: str, time_min: datetime, time_max: datetime) -> list:
        """返回 [(start, end), ...] 忙碌区间。实测:room_id 需放顶层,items 形式报 190002。"""
        body = {
            "time_min": time_min.astimezone().isoformat(),
            "time_max": time_max.astimezone().isoformat(),
            "room_id": room_id,
        }
        data = self._http("POST", "/open-apis/calendar/v4/freebusy/list", body=body).get("data", {})
        return data.get("freebusy_list", [])

    # ---------- 会议室日程(含会议主题/组织者,需要日历读取权限) ----------
    def room_events_today(self, room_id: str, now: datetime = None) -> list:
        now = now or datetime.now().astimezone()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        # _TUNABLE: start_time/end_time 参数格式(unix 秒 / RFC3339)以调试台为准
        data = self._http(
            "GET", f"/open-apis/calendar/v4/calendars/{room_id}/events",
            params={
                "start_time": str(int(day_start.timestamp())),
                "end_time": str(int(day_end.timestamp())),
                "page_size": "50",
            },
        ).get("data", {})
        return data.get("items", [])

    # ---------- 用户令牌(OAuth,oauth_setup.py 生成 token.json) ----------
    def _user_token(self) -> str:
        if not os.path.exists(TOKEN_PATH):
            raise FeishuError("缺少 token.json,请先运行 oauth_setup.py 完成扫码授权")
        with open(TOKEN_PATH, encoding="utf-8") as f:
            t = json.load(f)
        if time.time() < t["expires_at"] - 300:
            return t["access_token"]
        if time.time() > t.get("refresh_expires_at", 0):
            raise FeishuError("OAuth 授权已彻底过期,请重新运行 oauth_setup.py 扫码")
        # access_token 到期,用 refresh_token 续期(滚动续期,约 30 天有效)
        if not t.get("refresh_token"):
            raise FeishuError(
                "OAuth 令牌已过期且无 refresh_token(后台未开启刷新令牌能力),"
                "请重新运行 oauth_setup.py 扫码授权")
        req = urllib.request.Request(OAUTH_TOKEN_URL, data=json.dumps({
            "grant_type": "refresh_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "refresh_token": t["refresh_token"],
        }).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                r = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise FeishuError(
                f"OAuth 续期失败(HTTP {e.code}),请重新运行 oauth_setup.py 扫码: {detail[:200]}") from e
        if r.get("error"):
            raise FeishuError(f"OAuth 续期失败({r.get('error')}),请重新运行 oauth_setup.py")
        now = time.time()
        t.update({
            "access_token": r["access_token"],
            "refresh_token": r["refresh_token"],
            "expires_at": now + r.get("expires_in", 7200),
            "refresh_expires_at": now + r.get("refresh_token_expires_in", 2592000),
        })
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False, indent=2)
        return t["access_token"]

    def user_http(self, method: str, path: str, body=None, params=None) -> dict:
        """与 _http 相同,但用用户令牌(看到的是授权人账号的视角)"""
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", "Bearer " + self._user_token())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise FeishuError(f"HTTP {e.code} ({method} {path}): {detail}") from e
        if payload.get("code") != 0:
            raise FeishuError(f"[{payload.get('code')}] {payload.get('msg')}  ({method} {path})")
        return payload.get("data", {})

    def user_calendars(self) -> list:
        """列出授权人可见的全部日历(需在飞书客户端先订阅会议室日历)"""
        items, page_token = [], ""
        while True:
            data = self.user_http("GET", "/open-apis/calendar/v4/calendars",
                                  params={"page_size": "50", "page_token": page_token})
            items.extend(data.get("calendar_list", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")

    def calendar_events(self, calendar_id: str, day_start: datetime, day_end: datetime) -> list:
        items, page_token = [], ""
        while True:
            data = self.user_http(
                "GET", f"/open-apis/calendar/v4/calendars/{calendar_id}/events",
                params={"start_time": str(int(day_start.timestamp())),
                        "end_time": str(int(day_end.timestamp())),
                        "page_size": "50", "page_token": page_token})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")

    def tenant_events(self, calendar_id: str, day_start: datetime, day_end: datetime) -> list:
        """应用身份读日程(无需 OAuth、无 2 小时限制)。
        注意:与 user_token 不同,应用身份返回的周期日程是"母事件"(原始日期),
        需要调用方自行展开成当天实例(见 expand_recurrence)。"""
        items, page_token = [], ""
        while True:
            data = self._http(
                "GET", f"/open-apis/calendar/v4/calendars/{calendar_id}/events",
                params={"start_time": str(int(day_start.timestamp())),
                        "end_time": str(int(day_end.timestamp())),
                        "page_size": "50", "page_token": page_token}).get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")

    _booker_cache = {}      # event_id -> 预约人姓名(进程内缓存)

    def event_booker(self, calendar_id: str, event_id: str) -> str:
        """取日程的预约人:日程详情里的 event_organizer.display_name。
        (attendees 接口对非参会人返回 403,不能用;详情接口无此限制)"""
        if event_id in self._booker_cache:
            return self._booker_cache[event_id]
        name = None
        try:
            detail = self.user_http(
                "GET",
                f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}")
            ev = detail.get("event", detail)
            org = ev.get("event_organizer") or {}
            name = org.get("display_name")
            if not name and org.get("user_id"):
                name = self.user_name(org["user_id"])
        except FeishuError:
            pass
        self._booker_cache[event_id] = name or "已预约"
        return self._booker_cache[event_id]

    def user_name(self, open_id: str):
        """open_id -> 姓名(应用身份查通讯录,需 contact:user.base:readonly)"""
        data = self._http("GET", f"/open-apis/contact/v3/users/{open_id}",
                          params={"user_id_type": "open_id"}).get("data", {})
        return (data.get("user") or {}).get("name")


def build_room_data(freebusy_list: list, now: datetime = None) -> dict:
    """把 freebusy 忙碌区间转成 {status, bookings}(仅忙闲,无预约人)。
    bookings 元素:(时间段, "已预约", 是否当前进行中)"""
    now = now or datetime.now().astimezone()
    busy = False
    bookings = []
    for iv in freebusy_list:
        st = datetime.fromisoformat(iv["start_time"])
        et = datetime.fromisoformat(iv["end_time"])
        current = st <= now < et
        if current:
            busy = True
        if et <= now:
            continue                      # 已结束的不显示
        bookings.append((f"{st.strftime('%H:%M')}–{et.strftime('%H:%M')}", "已预约", current))
    return {"status": "busy" if busy else "free", "bookings": bookings}


def build_room_data_from_events(client: "FeishuClient", calendar_id: str,
                                events: list, now: datetime = None,
                                freebusy_list: list = None) -> dict:
    """把会议室日历的日程列表转成 {status, bookings},预约人取组织者姓名。
    清洗规则:
      - 跳过已取消(cancelled)和标记为"空闲"的日程
      - 跳过已结束的场次(门牌只关心当前和接下来)
      - 相同时间段的重复日程只保留一条
      - 传入 freebusy_list 时交叉校验:只保留与官方忙闲有交集的场次
    注意:v4 日程的时间是 unix 秒字符串(start_time.timestamp),不是 ISO 格式。"""
    now = now or datetime.now().astimezone()
    busy = False
    bookings = []
    seen = set()

    def in_freebusy(st, et) -> bool:
        if freebusy_list is None:
            return True
        for iv in freebusy_list:
            fs = datetime.fromisoformat(iv["start_time"])
            fe = datetime.fromisoformat(iv["end_time"])
            if st < fe and et > fs:        # 有交集
                return True
        return False

    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        if ev.get("free_busy_status") == "free":
            continue                      # 标记为"空闲"的日程不占会议室
        st_ts = (ev.get("start_time") or {}).get("timestamp")
        et_ts = (ev.get("end_time") or {}).get("timestamp")
        if not st_ts or not et_ts:
            continue                      # 全天日程等无精确时间的跳过
        st = datetime.fromtimestamp(int(st_ts)).astimezone()
        et = datetime.fromtimestamp(int(et_ts)).astimezone()
        if st <= now < et:
            busy = True
        if et <= now:
            continue                      # 已结束的不显示
        if not in_freebusy(st, et):
            continue                      # 不在官方忙闲里的不显示
        key = (st, et)
        if key in seen:
            continue                      # 重复时间段只显示一次
        seen.add(key)
        booker = client.event_booker(calendar_id, ev["event_id"])
        if booker == "已预约" and ev.get("summary"):
            booker = ev["summary"]      # 机器人预约常无人名但有主题,主题更有信息量
        bookings.append((f"{st:%H:%M}–{et:%H:%M}", booker))
    bookings.sort()
    return {"status": "busy" if busy else "free", "bookings": bookings}


# ---------- 周期日程展开(应用身份返回的是母事件,需要自行展开) ----------

_WEEKDAY_CODE = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _occurs_on(rule: dict, master_date, day) -> bool:
    """判断以 master_date 开始的周期规则在 day( date )这天是否有实例"""
    if day < master_date:
        return False
    until = rule.get("UNTIL")
    if until:
        # UNTIL 是 UTC,形如 20261231T155959Z
        until_dt = datetime.strptime(until[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        if day > until_dt.astimezone().date():
            return False
    freq = rule.get("FREQ")
    interval = int(rule.get("INTERVAL", "1"))
    if freq == "DAILY":
        return (day - master_date).days % interval == 0
    if freq == "WEEKLY":
        master_week = master_date - timedelta(days=master_date.weekday())
        day_week = day - timedelta(days=day.weekday())
        if ((day_week - master_week).days // 7) % interval != 0:
            return False
        byday = rule.get("BYDAY")
        codes = byday.split(",") if byday else [None]
        weekdays = {_WEEKDAY_CODE[c] for c in codes} if codes != [None] else {master_date.weekday()}
        return day.weekday() in weekdays
    if freq == "MONTHLY":
        months = (day.year - master_date.year) * 12 + day.month - master_date.month
        if months % interval != 0:
            return False
        byday = rule.get("BYDAY")
        if byday:
            for bd in byday.split(","):
                try:
                    ordinal, wd = int(bd[:-2]), _WEEKDAY_CODE[bd[-2:]]
                except (ValueError, KeyError):
                    return False
                if day.weekday() != wd:
                    continue
                if ordinal > 0 and (day.day - 1) // 7 + 1 == ordinal:
                    return True          # 第 N 个周 X
                if ordinal == -1 and (day + timedelta(days=7)).month != day.month:
                    return True          # 最后一个周 X
            return False
        return day.day == master_date.day
    return False                            # YEARLY 等不支持的规则:不展开(宁缺毋错)


def expand_recurrence(recurrence: str, master_st: datetime, master_et: datetime,
                      day_start: datetime, day_end: datetime) -> list:
    """把周期母事件展开为窗口内的 [(st, et), ...];解析失败返回 []"""
    try:
        rule = dict(p.split("=", 1) for p in recurrence.split(";") if "=" in p)
        results = []
        duration = master_et - master_st
        day = day_start.date()
        while day < day_end.date():
            if _occurs_on(rule, master_st.date(), day):
                st = datetime.combine(day, master_st.timetz())
                results.append((st, st + duration))
            day += timedelta(days=1)
        return results
    except Exception:
        return []


def build_room_data_from_tenant_events(events: list, freebusy_list: list,
                                       now: datetime = None,
                                       day_start: datetime = None,
                                       day_end: datetime = None,
                                       aliases: dict = None) -> dict:
    """应用身份日程 -> {status, bookings}。列表项自带 event_organizer.display_name。
    周期母事件自动展开;取消/空闲/已结束/与官方忙闲无交集的都不显示。
    aliases: 机器人日历 ID -> 显示名(机器人预约没有人名时按组织者日历映射)"""
    now = now or datetime.now().astimezone()
    busy = False
    bookings = []
    seen = set()

    def in_freebusy(st, et) -> bool:
        for iv in freebusy_list:
            fs = datetime.fromisoformat(iv["start_time"])
            fe = datetime.fromisoformat(iv["end_time"])
            if st < fe and et > fs:
                return True
        return False

    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        if ev.get("free_busy_status") == "free":
            continue
        ts = (ev.get("start_time") or {}).get("timestamp")
        te = (ev.get("end_time") or {}).get("timestamp")
        if not ts or not te:
            continue
        mst = datetime.fromtimestamp(int(ts)).astimezone()
        met = datetime.fromtimestamp(int(te)).astimezone()
        if ev.get("recurrence"):
            instances = expand_recurrence(ev["recurrence"], mst, met, day_start, day_end)
        else:
            instances = [(mst, met)]
        for st, et in instances:
            if not (day_start <= st < day_end):
                continue                  # 实例不在今天(接口会松散返回邻近事件)
            current = st <= now < et
            if current:
                busy = True
            if et <= now:
                continue                  # 已结束的不显示
            if not in_freebusy(st, et):
                continue
            if (st, et) in seen:
                continue                  # 同时段重复预订只显示一次
            seen.add((st, et))
            booker = ((ev.get("event_organizer") or {}).get("display_name")
                      or ev.get("summary")
                      or (aliases or {}).get(ev.get("organizer_calendar_id"))
                      or "已预约")
            bookings.append((f"{st:%H:%M}–{et:%H:%M}", booker, current))
    bookings.sort()
    return {"status": "busy" if busy else "free", "bookings": bookings}


if __name__ == "__main__":
    # 联调自测:python feishu_client.py  ->  打印 token 是否获取成功 + 第一个会议室的原始返回
    cfg = json.load(open("config.json", encoding="utf-8"))
    client = FeishuClient(cfg["app_id"], cfg["app_secret"])
    print("token 获取成功:", client.token()[:20], "...")
    rid = cfg["rooms"][0]["room_id"]
    now = datetime.now().astimezone()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    print("--- freebusy 原始返回 ---")
    print(json.dumps(client.freebusy(rid, day_start, day_start + timedelta(days=1)),
                     ensure_ascii=False, indent=2)[:2000])
    print("--- events 原始返回 ---")
    print(json.dumps(client.room_events_today(rid), ensure_ascii=False, indent=2)[:2000])
