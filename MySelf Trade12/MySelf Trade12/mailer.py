"""Myself Trade - 일일 보고서 메일.

매일 정해진 시각(기본 21시, 선택한 시간대 기준)에 그날의 매매 결과와 복기를 메일로 보낸다.
  - 내용: 하루 수익률, 순손익, 거래 횟수, 승률, 주요 거래, 잘한 점, 실수와 개선점, 다음 방침
  - 복기는 Gemini가 쓴다. Gemini 키가 없으면 통계 기반 요약으로 대신한다.
  - 발송 방식은 두 가지다.
      간편 발송: 받는 주소만 있으면 된다. 메일 전달 서비스(FormSubmit)가 대신 보내 준다.
      내 메일 계정: SMTP(예: Gmail)로 직접 보낸다. 보내는 계정의 주소와 '앱 비밀번호'가 필요하다.
"""
from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage

import requests

from config import APP_NAME, MODES, coin_meta, get_secret
from reports import build_report
from risk_manager import daily_limit_text
from strategy import rule_review
from utils import fmt_local, iso, local_day, log, to_local


SIMPLE_URL = "https://formsubmit.co/ajax/{email}"
SIMPLE_HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
                  "Origin": "http://127.0.0.1:8000", "Referer": "http://127.0.0.1:8000/",
                  "User-Agent": "Mozilla/5.0 (MyselfTrade daily report)"}


class MailError(Exception):
    pass


def mail_ready(settings) -> tuple[bool, str]:
    s = settings.all()
    if not s["mail_recipients"]:
        return False, "받는 메일 주소를 한 개 이상 추가해 주세요."
    if s["mail_method"] == "smtp":
        if not s["mail_from"]:
            return False, "보내는 메일 계정을 입력해 주세요."
        if not get_secret("MAIL_PASSWORD"):
            return False, "보내는 메일 계정의 앱 비밀번호를 저장해 주세요."
    return True, ""


# ---------------------------------------------------------------- 간편 발송 (주소만으로 보내기)
def send_simple(to: str, subject: str, fields: dict) -> dict:
    """메일 전달 서비스(FormSubmit)에 내용을 넘겨 to 주소로 보내게 한다.

    비밀번호나 2단계 인증이 필요 없다. 대신 주소마다 처음 한 번은 그 주소로 확인(Activate) 메일이 가고,
    받은 사람이 버튼을 눌러야 이후 메일이 전달된다.
    """
    payload = {"_subject": subject, "_template": "table", "_captcha": "false"}
    payload.update({key: str(value) for key, value in fields.items()})
    result = {"to": to, "ok": False, "pending": False, "message": ""}
    try:
        response = requests.post(SIMPLE_URL.format(email=requests.utils.quote(to, safe="@")), json=payload,
                                 headers=SIMPLE_HEADERS, timeout=25)
    except requests.RequestException as exc:
        result["message"] = f"메일 전달 서비스에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요. ({type(exc).__name__})"
        return result
    try:
        data = response.json()
    except ValueError:
        data = {}
    answer = str(data.get("message") or "").strip()
    if str(data.get("success")).lower() == "true":
        result.update(ok=True, message="발송했습니다.")
    elif "activat" in answer.lower():
        result.update(pending=True, message="이 주소로 확인 메일(FormSubmit, 제목에 Activate)이 갔습니다. 그 메일의 버튼을 한 번 누르면 "
                                             "다음부터 보고서가 도착합니다. 안 보이면 스팸함을 확인해 주세요.")
    else:
        result["message"] = f"메일 전달 서비스가 요청을 받지 않았습니다 (HTTP {response.status_code}). {answer[:200]}".strip()
    return result


# ---------------------------------------------------------------- 내 메일 계정으로 발송 (SMTP)
def send_smtp(settings, recipients: list[str], subject: str, text: str, html_body: str | None = None) -> None:
    s = settings.all()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{APP_NAME} <{s['mail_from']}>"
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    try:
        if int(s["smtp_port"]) == 465:
            server = smtplib.SMTP_SSL(s["smtp_host"], 465, timeout=25, context=context)
        else:
            server = smtplib.SMTP(s["smtp_host"], int(s["smtp_port"]), timeout=25)
            server.starttls(context=context)
        with server:
            server.login(s["mail_from"], get_secret("MAIL_PASSWORD"))
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("메일 서버 로그인에 실패했습니다. 보내는 계정 주소와 앱 비밀번호를 확인해 주세요. "
                        "(Gmail은 일반 비밀번호가 아니라 16자리 앱 비밀번호가 필요합니다)") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"메일을 보내지 못했습니다: {exc}") from exc


def deliver_mail(settings, recipients: list[str], subject: str, text: str, html_body: str | None, fields: dict) -> list[dict]:
    """받는 주소마다 발송 결과를 돌려준다: {"to", "ok", "pending"(확인 대기), "message"}"""
    ready, reason = mail_ready(settings)
    if not ready:
        raise MailError(reason)
    if settings.get("mail_method") == "smtp":
        try:
            send_smtp(settings, recipients, subject, text, html_body)
        except MailError as exc:
            return [{"to": to, "ok": False, "pending": False, "message": str(exc)} for to in recipients]
        return [{"to": to, "ok": True, "pending": False, "message": "발송했습니다."} for to in recipients]
    return [send_simple(to, subject, fields) for to in recipients]


def record_status(storage, results: list[dict], at_iso: str) -> None:
    """주소별 마지막 발송 결과를 저장한다 (메일 보고서 화면의 목록에 표시)."""
    status = storage.kv_get("mail_status") or {}
    for item in results:
        status[item["to"]] = {"ok": item["ok"], "pending": item["pending"], "message": item["message"], "at": at_iso}
    storage.kv_set("mail_status", status)


def summarize_results(results: list[dict]) -> str:
    sent = [r["to"] for r in results if r["ok"]]
    waiting = [r["to"] for r in results if r["pending"]]
    failed = [r for r in results if not r["ok"] and not r["pending"]]
    parts = []
    if sent:
        parts.append(f"{len(sent)}곳에 보냈습니다 ({', '.join(sent)}).")
    if waiting:
        parts.append(f"{len(waiting)}곳은 주소 확인이 필요합니다 ({', '.join(waiting)}). 그 주소로 온 확인 메일의 버튼을 한 번 눌러 주세요.")
    if failed:
        parts.append(f"{len(failed)}곳은 실패했습니다: {failed[0]['message']}")
    return " ".join(parts) or "보낼 주소가 없습니다."


# ---------------------------------------------------------------- 보고서 만들기
def _signed(value: float, digits: int = 2) -> str:
    return f"{value:+,.{digits}f}"


def _color(value: float) -> str:
    return "#D2342E" if value > 0 else "#2459C9" if value < 0 else "#18202F"   # 수익 빨강, 손실 파랑


def build_daily_report(storage, settings, analyzer, mode: str, now, equity_pnl_pct: float | None = None) -> dict:
    """오늘(선택한 시간대 기준) 보고서를 만든다. subject, text, html을 돌려준다."""
    tz = settings.get("timezone")
    day = local_day(now, tz)
    limits = daily_limit_text(settings.all())        # ('+30%', '-25%') 또는 금액으로 정했으면 ('+300 USDT', '-250 USDT')
    report = build_report(storage, mode, "1d", tz, now=now)
    week = build_report(storage, mode, "1w", tz, now=now)["summary"]
    month = build_report(storage, mode, "1m", tz, now=now)["summary"]
    s = report["summary"]
    pnl_pct = equity_pnl_pct if equity_pnl_pct is not None else s["return_pct"]
    raw_trades = storage.trades_on_day(mode, day)
    day_row = storage.get_day(mode, day) or {}
    open_trades = storage.open_trades(mode)

    # 복기: Gemini -> 실패하거나 키가 없으면 통계 요약
    review, source = None, "rule"
    if raw_trades and getattr(analyzer, "available", False):
        try:
            review = analyzer.daily_report(day, raw_trades, pnl_pct, day_row.get("plan"), storage.recent_lessons(15))
            source = "gemini"
        except Exception as exc:
            log.warning("Gemini 보고서 작성 실패, 통계 요약으로 대신합니다: %s", exc)
    if review is None:
        base = rule_review(day, raw_trades, pnl_pct)
        review = {"summary": base["summary"], "good": base.get("what_worked", []),
                  "mistakes": base.get("lessons", []), "improvements": [], "tomorrow": base.get("next_day_plan", "")}
        if not raw_trades:
            review["summary"] = f"{day}: 오늘은 완료된 거래가 없습니다."
            review["tomorrow"] = "진입 조건이 맞는 구간을 기다립니다."

    local_now = to_local(now, tz)
    title_day = f"{local_now.month}월 {local_now.day}일"
    subject = f"[{APP_NAME}] {title_day} 일일 매매 보고서 ({pnl_pct:+.2f}%)"
    ranked = sorted(report["trades"], key=lambda t: abs(t["net_pnl"] or 0), reverse=True)[:8]
    ranked.sort(key=lambda t: t["exit_time"])

    def bullets(items) -> list[str]:
        return [str(x).strip() for x in (items or []) if str(x).strip()]

    good, mistakes, improvements = bullets(review.get("good")), bullets(review.get("mistakes")), bullets(review.get("improvements"))

    # ----- 글자만 있는 본문
    lines = [
        f"{title_day} 매매 보고드립니다. ({MODES.get(mode, mode)})",
        "",
        "1. 오늘 결과",
        f"- 하루 수익률: {pnl_pct:+.2f}% (목표 {limits[0]}, 손실 한도 {limits[1]})",
        f"- 순손익: {_signed(s['net_pnl'])} USDT (수수료 {s['fee']:,.2f} USDT 차감 후)",
        f"- 거래: {s['trades']}회 (수익 {s['wins']}, 손실 {s['losses']}), 승률 {s['win_rate']:.1f}%",
        f"- 최근 7일 {week['return_pct']:+.2f}%, 최근 30일 {month['return_pct']:+.2f}%",
        "",
        "2. 요약",
        str(review.get("summary", "")),
        "",
        "3. 주요 거래",
    ]
    if ranked:
        for t in ranked:
            lines.append(f"- {t['exit_time'][6:]} {t['ticker']} {'롱' if t['side'] == 'long' else '숏'} {t['leverage']}배: "
                         f"{_signed(t['net_pnl'] or 0)} USDT ({(t['return_pct'] or 0):+.2f}%), {t['exit_reason']}")
    else:
        lines.append("- 완료된 거래가 없습니다.")
    for number, (heading, items) in enumerate((("잘한 점", good), ("실수와 개선점", mistakes + improvements)), start=4):
        lines += ["", f"{number}. {heading}"] + ([f"- {x}" for x in items] or ["- 특별히 적을 내용이 없습니다."])
    lines += ["", "6. 다음 방침", str(review.get("tomorrow", "")) or "-"]
    if open_trades:
        lines += ["", "보유 중인 포지션: " + ", ".join(
            f"{coin_meta(t['symbol'])['ticker']} {'롱' if t['side'] == 'long' else '숏'} {t['leverage']}배" for t in open_trades)]
    lines += ["", f"작성: {'Gemini' if source == 'gemini' else '통계 요약 (Gemini 키 없음)'}, "
                  f"{fmt_local(iso(now), tz, '%Y-%m-%d %H:%M')} ({tz})",
              "이 메일은 매매 기록을 정리한 것이며 투자 권유가 아닙니다."]
    text = "\n".join(lines)

    # ----- HTML 본문
    esc = html.escape
    cell = "padding:7px 10px;border-bottom:1px solid #E6EAF0;font-size:14px;text-align:left;"
    rows = "".join(
        f"<tr><td style='{cell}'>{esc(t['exit_time'][6:])}</td><td style='{cell}'>{esc(t['ticker'])}</td>"
        f"<td style='{cell}'>{'롱' if t['side'] == 'long' else '숏'} {t['leverage']}배</td>"
        f"<td style='{cell}text-align:right;color:{_color(t['net_pnl'] or 0)}'>{_signed(t['net_pnl'] or 0)}</td>"
        f"<td style='{cell}text-align:right;color:{_color(t['net_pnl'] or 0)}'>{(t['return_pct'] or 0):+.2f}%</td>"
        f"<td style='{cell}'>{esc(t['exit_reason'] or '')}</td></tr>" for t in ranked)
    table = (f"<table style='border-collapse:collapse;width:100%'><tr>"
             + "".join(f"<th style='{cell}text-align:{a};color:#667085;font-weight:500'>{h}</th>"
                       for h, a in (("청산", "left"), ("코인", "left"), ("포지션", "left"), ("순손익", "right"),
                                    ("수익률", "right"), ("사유", "left")))
             + f"</tr>{rows}</table>") if ranked else "<p style='color:#667085'>완료된 거래가 없습니다.</p>"

    def block(heading: str, items: list[str]) -> str:
        body = "".join(f"<li style='margin-bottom:5px'>{esc(x)}</li>" for x in items) or "<li>특별히 적을 내용이 없습니다.</li>"
        return f"<h3 style='font-size:15px;margin:22px 0 8px'>{heading}</h3><ul style='margin:0;padding-left:20px'>{body}</ul>"

    def stat(label: str, value: str, color: str = "#18202F") -> str:
        return (f"<td style='padding:10px 14px;border:1px solid #E6EAF0;text-align:left'><div style='font-size:12px;color:#667085'>{label}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{color}'>{value}</div></td>")

    html_body = f"""<div style="font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#18202F;max-width:640px;line-height:1.6">
<p style="margin:0 0 4px;color:#667085;font-size:13px">{esc(APP_NAME)} · {esc(MODES.get(mode, mode))}</p>
<h2 style="margin:0 0 14px;font-size:20px">{title_day} 매매 보고드립니다</h2>
<table style="border-collapse:collapse;width:100%"><tr>
{stat('하루 수익률', f'{pnl_pct:+.2f}%', _color(pnl_pct))}
{stat('순손익 (USDT)', _signed(s['net_pnl']), _color(s['net_pnl']))}
{stat('거래', f"{s['trades']}회")}
{stat('승률', f"{s['win_rate']:.1f}%")}
</tr></table>
<p style="margin:10px 0 0;font-size:13px;color:#667085">수수료 {s['fee']:,.2f} USDT 차감 후. 최근 7일 {week['return_pct']:+.2f}%, 최근 30일 {month['return_pct']:+.2f}%.
목표 {limits[0]}, 손실 한도 {limits[1]} (오늘 원금 기준).</p>
<h3 style="font-size:15px;margin:22px 0 8px">요약</h3><p style="margin:0">{esc(str(review.get('summary', '')))}</p>
<h3 style="font-size:15px;margin:22px 0 8px">주요 거래</h3>{table}
{block('잘한 점', good)}
{block('실수와 개선점', mistakes + improvements)}
<h3 style="font-size:15px;margin:22px 0 8px">다음 방침</h3><p style="margin:0">{esc(str(review.get('tomorrow', '')) or '-')}</p>
<p style="margin:24px 0 0;font-size:12px;color:#667085">작성: {'Gemini' if source == 'gemini' else '통계 요약 (Gemini 키 없음)'},
{esc(fmt_local(iso(now), tz, '%Y-%m-%d %H:%M'))} ({esc(tz)}). 이 메일은 매매 기록을 정리한 것이며 투자 권유가 아닙니다.</p>
</div>"""
    # ----- 간편 발송용: 항목 이름과 내용의 표로 전달된다
    fields = {
        "보고": f"{title_day} 매매 보고드립니다. ({MODES.get(mode, mode)})",
        "하루 수익률": f"{pnl_pct:+.2f}% (목표 {limits[0]}, 손실 한도 {limits[1]})",
        "순손익": f"{_signed(s['net_pnl'])} USDT (수수료 {s['fee']:,.2f} USDT 차감 후)",
        "거래": f"{s['trades']}회 (수익 {s['wins']}, 손실 {s['losses']}), 승률 {s['win_rate']:.1f}%",
        "최근 성과": f"7일 {week['return_pct']:+.2f}%, 30일 {month['return_pct']:+.2f}%",
        "요약": str(review.get("summary", "")),
        "주요 거래": "\n".join(
            f"{t['exit_time'][6:]} {t['ticker']} {'롱' if t['side'] == 'long' else '숏'} {t['leverage']}배: "
            f"{_signed(t['net_pnl'] or 0)} USDT ({(t['return_pct'] or 0):+.2f}%), {t['exit_reason']}" for t in ranked)
                    or "완료된 거래가 없습니다.",
        "잘한 점": "\n".join(f"- {x}" for x in good) or "특별히 적을 내용이 없습니다.",
        "실수와 개선점": "\n".join(f"- {x}" for x in mistakes + improvements) or "특별히 적을 내용이 없습니다.",
        "다음 방침": str(review.get("tomorrow", "")) or "-",
        "작성": f"{'Gemini' if source == 'gemini' else '통계 요약 (Gemini 키 없음)'}, "
                f"{fmt_local(iso(now), tz, '%Y-%m-%d %H:%M')} ({tz}). 매매 기록을 정리한 것이며 투자 권유가 아닙니다.",
    }
    return {"day": day, "subject": subject, "text": text, "html": html_body, "fields": fields, "source": source,
            "trades": s["trades"], "pnl_pct": pnl_pct}
