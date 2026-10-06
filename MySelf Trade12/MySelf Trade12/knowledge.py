"""Myself Trade - 학습 자료 (지식 베이스).

사용자가 대시보드의 '학습 자료' 화면에 올린 전략 글, 유튜브 링크, 차트 이미지를 보관하고,
Gemini로 매매 규칙을 뽑아 이후의 모든 계획/판단 프롬프트에 함께 넣는다.

- 글: Gemini 키가 없어도 저장된다. 키가 없으면 원문 그대로, 있으면 규칙으로 정리해서 쓴다.
- 유튜브 링크, 차트 이미지: Gemini가 직접 보고 정리해야 하므로 키가 필요하다. 키가 없으면 '분석 대기'로 두었다가
  키를 저장하면 그때 분석한다.
- Gemini 앱(gemini.google.com)의 대화나 그곳에 올린 파일은 API로 읽을 수 없다. 자료는 이 화면에 올려야 한다.
"""
from __future__ import annotations

import base64
import re
import threading
from pathlib import Path

from utils import iso, log, utcnow

MAX_TEXT = 20_000                  # 글 한 건의 최대 길이(글자)
MAX_IMAGE_BYTES = 7 * 1024 * 1024  # 이미지 한 장의 최대 크기
MAX_ITEMS = 40                     # 보관할 수 있는 자료 수
PROMPT_BUDGET = 7000               # 프롬프트에 넣을 자료의 최대 길이(글자)
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif"}
KIND_KO = {"text": "전략 글", "youtube": "유튜브", "image": "차트 이미지"}
_YOUTUBE = re.compile(r"^https://(www\.|m\.)?(youtube\.com/(watch\?[^\s]*v=[\w-]{6,}|shorts/[\w-]{6,}|live/[\w-]{6,})|youtu\.be/[\w-]{6,})[^\s]*$")

_work_lock = threading.Lock()      # 자료 분석은 한 번에 하나씩


class MaterialError(ValueError):
    """자료를 받을 수 없을 때 (형식, 크기 등)."""


def _folder(storage) -> Path:
    folder = Path(storage.folder) / "materials"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add_material(storage, kind: str, title: str = "", text: str = "", url: str = "",
                 image_b64: str = "", mime: str = "", filename: str = "") -> int:
    """자료를 저장하고 번호를 돌려준다. 분석은 process_material이 따로 한다."""
    if len(storage.materials()) >= MAX_ITEMS:
        raise MaterialError(f"자료는 {MAX_ITEMS}개까지 보관할 수 있습니다. 쓰지 않는 자료를 지운 뒤 다시 올려 주세요.")
    title, text = str(title or "").strip()[:80], str(text or "").strip()
    if len(text) > MAX_TEXT:
        raise MaterialError(f"글이 너무 깁니다. {MAX_TEXT:,}자 이하로 줄여 주세요 (지금 {len(text):,}자).")
    fields = {"created_at": iso(utcnow()), "kind": kind, "status": "pending", "content": text}
    if kind == "text":
        if len(text) < 10:
            raise MaterialError("전략 내용을 10자 이상 적어 주세요.")
        fields.update(title=title or text.splitlines()[0][:40], source="")
        return storage.add_material(**fields)
    if kind == "youtube":
        url = str(url or "").strip()
        if not _YOUTUBE.match(url):
            raise MaterialError("유튜브 영상 주소가 아닙니다. https://www.youtube.com/watch?v=... 또는 https://youtu.be/... 형태로 넣어 주세요.")
        fields.update(title=title or "유튜브 영상", source=url)
        return storage.add_material(**fields)
    if kind == "image":
        mime = str(mime or "").lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in IMAGE_TYPES:
            raise MaterialError("이미지는 PNG, JPG, WEBP, HEIC 파일만 올릴 수 있습니다.")
        try:
            data = base64.b64decode(str(image_b64 or "").split(",")[-1], validate=True)
        except (ValueError, TypeError) as exc:
            raise MaterialError("이미지 파일을 읽지 못했습니다. 다시 골라 주세요.") from exc
        if not data:
            raise MaterialError("이미지 파일이 비어 있습니다.")
        if len(data) > MAX_IMAGE_BYTES:
            raise MaterialError(f"이미지가 너무 큽니다 ({len(data) / 1048576:.1f}MB). 7MB 이하로 줄여 주세요.")
        fields.update(title=title or Path(str(filename or "차트 이미지")).stem[:40] or "차트 이미지", source=str(filename or "")[:120])
        material_id = storage.add_material(**fields)
        name = f"{material_id}{IMAGE_TYPES[mime]}"
        (_folder(storage) / name).write_bytes(data)
        storage.update_material(material_id, file=name)
        return material_id
    raise MaterialError("자료 종류를 알 수 없습니다.")


def remove_material(storage, material_id: int) -> None:
    row = storage.get_material(material_id)
    if row and row.get("file"):
        try:
            (_folder(storage) / row["file"]).unlink(missing_ok=True)
        except OSError:
            pass
    storage.delete_material(material_id)


def process_material(storage, analyzer, material_id: int) -> dict | None:
    """자료 하나를 Gemini로 분석해 규칙을 저장한다. 키가 없으면 그대로 두고(글은 원문으로 쓰인다) 돌려준다."""
    row = storage.get_material(material_id)
    if row is None or not getattr(analyzer, "available", False) or not hasattr(analyzer, "digest_material"):
        return row
    with _work_lock:
        try:
            if row["kind"] == "image":
                path = _folder(storage) / (row.get("file") or "")
                mime = next((m for m, ext in IMAGE_TYPES.items() if path.suffix == ext), "image/png")
                summary = analyzer.digest_material("image", row["title"], row.get("content") or "",
                                                   image_b64=base64.b64encode(path.read_bytes()).decode("ascii"), mime=mime)
            elif row["kind"] == "youtube":
                summary = analyzer.digest_material("youtube", row["title"], row.get("content") or "", url=row["source"])
            else:
                summary = analyzer.digest_material("text", row["title"], row.get("content") or "")
            storage.update_material(material_id, summary=summary, status="ok", error="")
            log.info("학습 자료 분석 완료: %s (규칙 %d개)", row["title"], len(summary.get("rules", [])))
        except Exception as exc:
            message = str(exc)
            if row["kind"] == "youtube":
                message += " (공개 영상만 읽을 수 있습니다. 비공개나 일부 공개 영상, 너무 긴 영상은 실패할 수 있습니다. 영상의 핵심을 글로 적어 '전략 글'로 올리면 확실합니다.)"
            storage.update_material(material_id, status="error", error=message[:600])
            log.warning("학습 자료 분석 실패 (%s): %s", row["title"], exc)
    return storage.get_material(material_id)


def process_pending(storage, analyzer) -> int:
    """분석을 기다리는 자료를 모두 처리한다 (Gemini 키를 새로 저장했을 때)."""
    done = 0
    for row in reversed(storage.materials()):
        if row["status"] == "pending":
            process_material(storage, analyzer, row["id"])
            done += 1
    return done


def process_async(storage, analyzer, material_id: int | None = None) -> None:
    target = (lambda: process_material(storage, analyzer, material_id)) if material_id else (lambda: process_pending(storage, analyzer))
    threading.Thread(target=target, name="materials", daemon=True).start()


def _blocks(storage) -> list[tuple[int, str]]:
    """프롬프트에 실제로 들어가는 자료 (번호, 글). 최신 자료부터, 정해진 길이 안에 들어가는 것만."""
    blocks, used = [], 0
    for row in storage.materials(only_enabled=True):
        summary = row.get("summary") or {}
        head = f"[{KIND_KO.get(row['kind'], row['kind'])}] {row['title']}"
        if row["status"] == "ok" and (summary.get("rules") or summary.get("summary")):
            lines = [head + ": " + summary.get("summary", "")]
            lines += [f"  - {rule}" for rule in summary.get("rules", [])]
            if summary.get("long_conditions"):
                lines.append("  롱 조건: " + " / ".join(summary["long_conditions"]))
            if summary.get("short_conditions"):
                lines.append("  숏 조건: " + " / ".join(summary["short_conditions"]))
            if summary.get("cautions"):
                lines.append("  주의: " + " / ".join(summary["cautions"]))
            block = "\n".join(lines)
        elif row["kind"] == "text" and row.get("content"):
            block = head + " (사용자가 적은 원문): " + row["content"][:1500]      # 아직 정리 전이면 원문을 그대로 쓴다
        elif row.get("content"):
            block = head + " (자료는 아직 분석 전, 사용자 메모만): " + row["content"][:600]
        else:
            continue
        if used + len(block) > PROMPT_BUDGET:
            continue                     # 이 자료는 길어서 못 넣는다. 더 짧은 다음 자료는 들어갈 수 있다.
        blocks.append((row["id"], block))
        used += len(block)
    return blocks


def knowledge_text(storage) -> str:
    """켜져 있는 자료를 프롬프트에 넣을 글로 만든다."""
    return "\n".join(block for _, block in _blocks(storage))


def used_ids(storage) -> set[int]:
    """지금 AI 프롬프트에 실제로 들어가는 자료의 번호."""
    return {material_id for material_id, _ in _blocks(storage)}


def material_view(row: dict, tz: str, used: set[int] | None = None) -> dict:
    """화면용. used: 지금 프롬프트에 실제로 들어가는 자료 번호 (used_ids)."""
    from utils import fmt_local
    summary = row.get("summary") or {}
    in_use = row["id"] in used if used is not None else bool(row["enabled"])
    return {"id": row["id"], "kind": row["kind"], "kind_label": KIND_KO.get(row["kind"], row["kind"]), "title": row["title"],
            "source": row.get("source") or "", "note": (row.get("content") or "")[:400], "status": row["status"],
            "error": row.get("error") or "", "enabled": bool(row["enabled"]), "in_use": in_use,
            "created": fmt_local(row["created_at"], tz, "%m-%d %H:%M"), "summary": summary.get("summary", ""),
            "rules": summary.get("rules", []), "long_conditions": summary.get("long_conditions", []),
            "short_conditions": summary.get("short_conditions", []), "cautions": summary.get("cautions", [])}
