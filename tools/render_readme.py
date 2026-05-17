#!/usr/bin/env python3
import sys, re, os

RESET  = "\033[0m"
BLUE   = "\033[96m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
PURPLE = "\033[95m"
CORAL  = "\033[91m"
DIM    = "\033[90m"
BOLD   = "\033[1m"

def inline(text):
    text = re.sub(r'`([^`]+)`', lambda m: f"{BLUE}{m.group(1)}{RESET}", text)
    text = re.sub(r'\*\*([^*]+)\*\*', lambda m: f"{PURPLE}{BOLD}{m.group(1)}{RESET}", text)
    return text

def box(title):
    width = 39
    pad = " " * max(0, width - len(title) - 8)
    top    = f"  {DIM}{'▄' * width}{RESET}"
    mid    = f"  {DIM}█ {RESET}{BLUE}{BOLD}⬡  {title}{RESET}{pad}{DIM} █{RESET}"
    bottom = f"  {DIM}{'▀' * width}{RESET}"
    return "\n".join([top, mid, bottom])

def render(path):
    lines = open(path).readlines()
    out = []

    for line in lines:
        line = line.rstrip()

        if re.match(r'^# ', line):
            out += ["", box(line[2:].strip()), ""]

        elif re.match(r'^### ', line):
            out.append(f"  {YELLOW}{line[4:].strip()}{RESET}")

        elif m := re.match(r'^- \*\*([^*]+)\*\*\s*(.*)', line):
            out.append(f"    {GREEN}●{RESET}  {PURPLE}{BOLD}{m.group(1)}{RESET}  {inline(m.group(2))}")

        elif m := re.match(r'^(\d+)\. (.*)', line):
            out.append(f"    {CORAL}{m.group(1)}.{RESET}  {inline(m.group(2))}")

        elif not line.strip():
            out.append("")

        else:
            out.append(f"    {inline(line)}")

    out += [f"  {DIM}{'─' * 40}{RESET}", f"  {GREEN}${RESET} {DIM}ready.{RESET}", ""]
    print("\n".join(out))

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "../README.md")
render(path)
