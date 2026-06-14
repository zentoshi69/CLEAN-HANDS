"""Inline game.css + game.js into one portable file you can open in any browser.
Run: python3 build_standalone.py  ->  standalone.html (no server, saves to localStorage)."""
import os
D = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(D, "index.html")).read()
css = open(os.path.join(D, "game.css")).read()
js = open(os.path.join(D, "game.js")).read()
html = html.replace('<link rel="stylesheet" href="game.css" />', "<style>\n" + css + "\n</style>")
html = html.replace('<script src="game.js"></script>', "<script>\n" + js + "\n</script>")
open(os.path.join(D, "standalone.html"), "w").write(html)
print("wrote standalone.html:", len(html), "bytes")
