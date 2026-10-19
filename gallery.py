"""Public gallery presentation and administrator-owned site categories."""
from datetime import datetime, timezone
import html
from pathlib import Path
from string import Template


SCHEMA = """
CREATE TABLE IF NOT EXISTS hatchery_gallery_categories(
    slug TEXT PRIMARY KEY,
    is_partner BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hatchery_gallery_migrations(
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""
INITIAL_PARTNERS = (
    "cogito", "racingbusiness", "sdszrootca", "sdszemergingmarketssociety",
)


def initialize(conn, existing_slugs):
    """Seed only the four existing clubs, once, in the caller's transaction.

    An absent club must not become a partner when somebody later takes its slug.
    Restarts must also preserve an administrator's decision to move a club out.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute(
            "INSERT INTO hatchery_gallery_migrations(name,applied_at) VALUES(%s,%s) "
            "ON CONFLICT (name) DO NOTHING RETURNING name",
            ("initial-partner-clubs-20261019", now),
        )
        if cur.fetchone():
            # A developer checkout may share the index without mirroring production
            # files. Registered websites still count as existing in that case.
            cur.execute("SELECT slug FROM hatchery_published_sites")
            existing_slugs = set(existing_slugs) | {row[0] for row in cur.fetchall()}
            for slug in INITIAL_PARTNERS:
                if slug in existing_slugs:
                    set_category(cur, slug, True, None, now)


def categories(cur):
    cur.execute("SELECT slug, is_partner FROM hatchery_gallery_categories")
    return {row["slug"]: bool(row["is_partner"]) for row in cur.fetchall()}


def set_category(cur, slug, is_partner, admin_id, now):
    cur.execute(
        "INSERT INTO hatchery_gallery_categories(slug,is_partner,updated_by,updated_at) VALUES(%s,%s,%s,%s) "
        "ON CONFLICT (slug) DO UPDATE SET is_partner=EXCLUDED.is_partner, "
        "updated_by=EXCLUDED.updated_by, updated_at=EXCLUDED.updated_at",
        (slug, is_partner, admin_id, now),
    )


def move_category(cur, old_slug, new_slug):
    if old_slug == new_slug:
        return
    # New sites never inherit a stale classification from an old occupant.
    cur.execute("DELETE FROM hatchery_gallery_categories WHERE slug = %s", (new_slug,))
    if old_slug:
        cur.execute("UPDATE hatchery_gallery_categories SET slug = %s WHERE slug = %s", (new_slug, old_slug))


ARROW = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 18 18 6M6 6h12v12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
# Public homepage snapshots captured 2026-10-19; these are static images, not live iframes.
PREVIEW_ASSETS = {slug: f'/gallery-previews/{slug}.jpg' for slug in INITIAL_PARTNERS}


def card(site, asset_version):
    esc = lambda value: html.escape(str(value), quote=True)
    slug = str(site["slug"])
    name = site["siteName"] or slug
    description = site["description"] or ""
    url = str(site["publicUrl"])
    partner = bool(site.get("isPartner"))
    kind = "partner" if partner else "other"
    search = esc(" ".join((name, description, slug, site.get("owner") or "")))
    summary = f'<p class="site-description">{esc(description)}</p>' if description else ""
    initial = esc(name[:2].upper() if name[:1].isascii() else name[:1])
    preview = PREVIEW_ASSETS.get(slug)
    if partner and preview:
        visual = (f'<div class="site-preview"><img src="{preview}?v={asset_version(preview)}" '
                  f'width="864" height="486" loading="lazy" decoding="async" alt=""></div>')
    elif partner:
        visual = f'<div class="site-preview preview-placeholder" aria-hidden="true"><span>{initial}</span></div>'
    else:
        visual = f'<span class="site-monogram" aria-hidden="true">{initial}</span>'
    return (
        f'<a class="site-card {kind}-card" data-gallery-card data-search="{search}" '
        f'href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
        f'{visual}<div class="site-card-body">'
        f'<div class="site-card-title"><h3>{esc(name)}</h3><span class="visit-icon">{ARROW}</span></div>'
        f'{summary}</div></a>'
    )


def render(sites, frontend: Path, asset_version):
    public = [site for site in sites if site["listed"] and site["onDisk"]]
    partners = [site for site in public if site.get("isPartner")]
    others = [site for site in public if not site.get("isPartner")]
    return Template((frontend / "gallery.html").read_text(encoding="utf-8")).substitute(
        css_version=asset_version("/gallery.css"),
        js_version=asset_version("/gallery.js"),
        partner_cards="".join(card(site, asset_version) for site in partners),
        other_cards="".join(card(site, asset_version) for site in others),
        partner_empty="" if not partners else " hidden",
        other_empty="" if not others else " hidden",
    )
