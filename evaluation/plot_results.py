import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Plot Static vs Adaptive SDN Load Balancer Comparison Charts")
    parser.add_argument("--input", default="results/benchmark_results.json", help="Input benchmark JSON")
    parser.add_argument("--out-dir", default="results", help="Output directory for PNG charts")
    args = parser.parse_args()

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[!] matplotlib is not installed; skipping PNG chart generation.", file=sys.stderr)
        return

    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    os.makedirs(args.out_dir, exist_ok=True)
    scenarios = list(data.keys())
    static_rtt = [data[s]["Static_RR"]["mean_rtt_ms"] for s in scenarios]
    adapt_rtt = [data[s]["Adaptive_SDN"]["mean_rtt_ms"] for s in scenarios]
    static_succ = [data[s]["Static_RR"]["success_rate_pct"] for s in scenarios]
    adapt_succ = [data[s]["Adaptive_SDN"]["success_rate_pct"] for s in scenarios]
    static_tp = [data[s]["Static_RR"]["throughput_rps"] for s in scenarios]
    adapt_tp = [data[s]["Adaptive_SDN"]["throughput_rps"] for s in scenarios]

    x = range(len(scenarios))
    width = 0.35

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))

    axes[0].bar([i - width / 2 for i in x], static_rtt, width, label="Static RR", color="#d9534f")
    axes[0].bar([i + width / 2 for i in x], adapt_rtt, width, label="Adaptive SDN", color="#2e86de")
    axes[0].set_title("Mean Response Time (Lower is Better)")
    axes[0].set_ylabel("Mean RTT (ms)")
    axes[0].set_xticks(list(x))
    axes[0].set_xticklabels(scenarios, rotation=20, ha="right")
    axes[0].legend()

    axes[1].bar([i - width / 2 for i in x], static_succ, width, label="Static RR", color="#d9534f")
    axes[1].bar([i + width / 2 for i in x], adapt_succ, width, label="Adaptive SDN", color="#2e86de")
    axes[1].set_title("Success Rate (Higher is Better)")
    axes[1].set_ylabel("Success (%)")
    axes[1].set_ylim(0, 110)
    axes[1].set_xticks(list(x))
    axes[1].set_xticklabels(scenarios, rotation=20, ha="right")
    axes[1].legend()

    axes[2].bar([i - width / 2 for i in x], static_tp, width, label="Static RR", color="#d9534f")
    axes[2].bar([i + width / 2 for i in x], adapt_tp, width, label="Adaptive SDN", color="#2e86de")
    axes[2].set_title("Throughput (Higher is Better)")
    axes[2].set_ylabel("Requests / sec")
    axes[2].set_xticks(list(x))
    axes[2].set_xticklabels(scenarios, rotation=20, ha="right")
    axes[2].legend()

    plt.tight_layout()
    chart_path = os.path.join(args.out_dir, "comparison_charts.png")
    plt.savefig(chart_path, dpi=150)
    print(f"[*] Saved comparison chart to {chart_path}")


if __name__ == "__main__":
    main()
