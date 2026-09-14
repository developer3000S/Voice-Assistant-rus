import io, re, sys, tokenize

FILES = sys.argv[1:] or [
    "actions/web_search.py", "actions/flight_finder.py", "actions/youtube_video.py",
    "actions/desktop.py", "actions/computer_control.py",
]

CYR = re.compile(r'[а-яА-ЯёЁ]')

for fn in FILES:
    src = io.open(fn, encoding="utf-8").read()
    print(f"\n===== {fn} =====")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING:
            raw = tok.string
            body = re.sub(r'^[rbfuRBFU]*["\']{1,3}', '', raw)
            body = re.sub(r'["\']{1,3}$', '', body)
            if CYR.search(body):
                continue
            # skip pure identifiers / keys / codes / urls / formats
            if len(body.strip()) < 4:
                continue
            if re.fullmatch(r'[\w\s./:\\%\-+,;:()\[\]{}\'"<>=&?#*]+', body) and ' ' not in body.strip():
                continue
            print(f"  L{tok.start[0]:>4}: {body[:110]!r}")
        elif tok.type == tokenize.COMMENT:
            c = tok.string.lstrip('#').strip()
            if c and not CYR.search(c) and len(c) > 3 and 'pylint' not in c:
                print(f"  CMT L{tok.start[0]:>4}: {c[:110]!r}")
