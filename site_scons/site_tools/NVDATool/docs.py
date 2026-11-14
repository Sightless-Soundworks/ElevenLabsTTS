import gettext
import re
from pathlib import Path

import markdown

from .typings import AddonInfo


def md2html(
	source: str | Path,
	dest: str | Path,
	*,
	moFile: str | Path | None,
	mdExtensions: list[str],
	addon_info: AddonInfo,
	cssFile: str | Path | None = None,
):
	if isinstance(source, str):
		source = Path(source)
	if isinstance(dest, str):
		dest = Path(dest)
	if isinstance(moFile, str):
		moFile = Path(moFile)
	if isinstance(cssFile, str):
		cssFile = Path(cssFile)

	try:
		with moFile.open("rb") as f:
			_ = gettext.GNUTranslations(f).gettext
	except Exception:
		summary = addon_info["addon_summary"]
	else:
		summary = _(addon_info["addon_summary"])
	version = addon_info["addon_version"]
	title = f"{summary} {version}"
	lang = source.parent.name.replace("_", "-")
	headerDic = {
		'[[!meta title="': "# ",
		'"]]': " #",
	}
	with source.open("r", encoding="utf-8") as f:
		mdText = f.read()
	for k, v in headerDic.items():
		mdText = mdText.replace(k, v, 1)
	htmlText = markdown.markdown(mdText, extensions=mdExtensions)
	# The stylesheet goes into the page, so the add-on ships no separate file.
	style = ""
	if cssFile is not None:
		# utf-8-sig drops a byte order mark; @charset has no meaning inside a page.
		css = re.sub(r'^\s*@charset\s+"[^"]*"\s*;\s*', "", cssFile.read_text(encoding="utf-8-sig"))
		style = f"<style>\n{css.strip()}\n</style>"
	# Optimization: build resulting HTML text in one go instead of writing parts separately.
	docText = "\n".join(
		(
			"<!DOCTYPE html>",
			f'<html lang="{lang}">',
			"<head>",
			'<meta charset="UTF-8">',
			'<meta name="viewport" content="width=device-width, initial-scale=1.0">',
			*((style,) if style else ()),
			f"<title>{title}</title>",
			"</head>\n<body>",
			htmlText,
			"</body>\n</html>",
		),
	)
	with dest.open("w", encoding="utf-8") as f:
		f.write(docText)  # type: ignore
