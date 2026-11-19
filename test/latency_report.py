"""Summarize Hub and speaker turn_latency logs without reproducing their content."""
import argparse
from collections import defaultdict
import math
import re


EVENT = re.compile(r'\bturn_latency id=([a-f0-9]{32}) stage=([a-z_]+) ms=(\d+)\b')
STAGES = set('asr model_first_text first_opus_sent direct_result tool_finished mcp_connect_cold mcp_connected mcp_total speech_end_to_stt stt_to_output speech_end_to_output'.split())
ROUTES = {'route_clock', 'route_weather', 'route_home_assistant', 'route_model'}


def summarize(lines):
    turns = defaultdict(lambda: defaultdict(list))
    for line in lines:
        match = EVENT.search(line)
        if match and match[2] in STAGES | ROUTES:
            turns[match[1]][match[2]].append(int(match[3]))
    groups = defaultdict(list)
    for events in turns.values():
        route = next((key[6:] for key in events if key in ROUTES), 'unclassified')
        if 'mcp_connect_cold' in events:
            route += '/mcp-cold'
        elif 'mcp_connected' in events:
            route += '/mcp-connected'
        for stage, values in events.items():
            groups[(route, stage)].extend(values)
    for (route, stage), values in sorted(groups.items()):
        values.sort()
        yield route, stage, len(values), values[math.ceil(len(values)*.5)-1], values[math.ceil(len(values)*.95)-1]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', nargs='+', type=argparse.FileType('r', encoding='utf-8', errors='replace'))
    args = parser.parse_args()
    print('route stage n p50_ms p95_ms')
    for row in summarize(line for stream in args.logs for line in stream):
        print(*row)
