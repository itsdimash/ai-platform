import re

import pytest

from app.utils.storage import (
    build_key,
    can_access_key,
    content_disposition,
    display_name,
    is_inline_mime,
    is_valid_key,
    make_record,
    owns_key,
    safe_name,
)

KEY_RE = re.compile(r"^ai/(\d+)/(\d+|_pending)/(uploads/)?[0-9a-f]{32}_.+$")


def test_build_key_format_generated_and_upload():
    key = build_key(7, 42, "Отчёт Q3.pptx")
    assert KEY_RE.match(key) and key.startswith("ai/7/42/")
    assert "uploads/" not in key
    assert key.endswith("_Отчёт_Q3.pptx")

    up = build_key(7, 42, "scan.png", uploads=True)
    assert up.startswith("ai/7/42/uploads/") and KEY_RE.match(up)

    pending = build_key(7, None, "a.pdf", uploads=True)
    assert pending.startswith("ai/7/_pending/uploads/")


def test_build_key_is_unique_and_128_bit():
    keys = {build_key(1, 1, "a.txt") for _ in range(50)}
    assert len(keys) == 50


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("a/b\\c.docx", "c.docx"),
        ("my file (1).XLSX", "my_file_1.xlsx"),
        ("", "file"),
        ("....", "file"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_safe_name_is_length_limited_and_keeps_extension():
    name = safe_name("x" * 300 + ".pptx")
    assert len(name) <= 80 and name.endswith(".pptx")


def test_display_name_strips_path_and_forbidden_chars():
    assert display_name('a/b:c*"d?.docx') == "b c d.docx"
    assert display_name("a/b.docx") == "b.docx"
    assert display_name("   ") == "file"
    assert display_name("Годовой отчёт.pdf") == "Годовой отчёт.pdf"


@pytest.mark.parametrize(
    "key",
    [
        "ai/1/2/abc_f.txt",
        "ai/1/_pending/uploads/abc_f.txt",
    ],
)
def test_valid_keys(key):
    assert is_valid_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "documents/abc_f.docx",  # вне ai/
        "ai/1/2",  # слишком коротко
        "ai/1/../2/abc_f.txt",
        "ai/1/2/../../2/f.txt",
        "/ai/1/2/f.txt",
        "ai//1/2/f.txt",
        "ai/1/2/f.txt/",
        "ai/1\\2/f.txt",
        "ai/1/2/f\x00.txt",
        "ai/1/2/" + "a" * 600,
    ],
)
def test_invalid_keys(key):
    assert not is_valid_key(key)


def test_owner_access_matrix():
    mine = "ai/7/42/abc_f.txt"
    other = "ai/8/42/abc_f.txt"
    assert owns_key(mine, 7) and not owns_key(other, 7)
    assert can_access_key(mine, 7, "pm")
    assert not can_access_key(other, 7, "pm")
    # префикс-сосед: user 7 не должен читать user 70
    assert not can_access_key("ai/70/1/abc_f.txt", 7, "pm")
    assert not can_access_key("ai/_selftest/abc.txt", 7, "pm")


@pytest.mark.parametrize("role", ["admin", "commercial_director"])
def test_privileged_roles_read_any_ai_key(role):
    assert can_access_key("ai/8/42/abc_f.txt", 7, role)
    assert can_access_key("ai/_selftest/x/y.txt", 7, role)


@pytest.mark.parametrize("role", ["admin", "commercial_director"])
def test_privileged_roles_do_not_escape_ai_prefix(role):
    assert not can_access_key("documents/secret.docx", 7, role)
    assert not can_access_key("ai/8/../../erp/x.txt", 7, role)
    # привязка к своему сообщению — строго собственные ключи даже для admin
    assert not owns_key("ai/8/42/abc_f.txt", 7)


def test_inline_mime():
    assert is_inline_mime("image/png") and is_inline_mime("application/pdf")
    assert not is_inline_mime("image/svg+xml")
    assert not is_inline_mime("text/html")
    assert not is_inline_mime(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_content_disposition_rfc5987():
    header = content_disposition("Годовой отчёт.pdf", inline=False)
    assert header.startswith("attachment; filename=")
    assert 'filename="' in header
    fallback = re.search(r'filename="([^"]*)"', header).group(1)
    assert fallback.isascii() and fallback.endswith(".pdf")
    assert "filename*=UTF-8''%D0%93%D0%BE%D0%B4%D0%BE%D0%B2%D0%BE%D0%B9" in header

    assert content_disposition("photo.png", inline=True).startswith("inline; ")
    # кавычки и переводы строк не ломают заголовок
    nasty = content_disposition('a"b\r\nSet-Cookie: x.pdf', inline=False)
    assert "\r" not in nasty and "\n" not in nasty
    assert nasty.count('"') == 2


def test_make_record_shape():
    rec = make_record(name="a/b.png", key="ai/1/2/x_b.png", mime="image/png", size=10)
    assert rec == {
        "type": "image",
        "name": "b.png",
        "key": "ai/1/2/x_b.png",
        "mime": "image/png",
        "size": 10,
    }
    assert make_record(name="a.pdf", key="k", mime="application/pdf", size=1)["type"] == "file"
