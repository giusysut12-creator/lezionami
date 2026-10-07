"""Assembla artefatto/lezionami.html da sorgente.html e dall'illustrazione dell'app."""
from pathlib import Path

base = Path(__file__).resolve().parent
svg = (base.parent / "app" / "static" / "illustrazione.svg").read_text(encoding="utf-8")
html = (base / "sorgente.html").read_text(encoding="utf-8").replace("{{ILLUSTRAZIONE}}", svg.strip())
(base / "lezionami.html").write_text(html, encoding="utf-8")
print(f"Scritto {base / 'lezionami.html'} ({len(html):,} caratteri)")
