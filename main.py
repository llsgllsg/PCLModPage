#!/usr/bin/env python3
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import os
import time

import modrinth_api

LABEL = "Minecraft 模组推荐"
COLUMNS = 2
TEMPLATE_DIR = "templates"


def escape_xaml(text) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def replaces(template: str, data: dict, no_escape_keys=None) -> str:
    if no_escape_keys is None:
        no_escape_keys = []
    for key, value in data.items():
        if key in no_escape_keys:
            template = template.replace("{" + key + "}", str(value))
        else:
            template = template.replace("{" + key + "}", escape_xaml(value))
    return template


def fmt_count(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def build_stats_xaml(mod: dict) -> str:
    d = fmt_count(mod["downloads"])
    f = fmt_count(mod["follows"])
    v = mod.get("version") or ""
    text = escape_xaml(f"下载 {d} · 关注 {f}" + (f" · MC {v}" if v else ""))
    return (
        f'<TextBlock Margin="0,6,0,0" HorizontalAlignment="Center" TextAlignment="Center" '
        f'VerticalAlignment="Center" FontSize="14" FontWeight="Bold" '
        f'Foreground="{{DynamicResource ColorBrush3}}" TextWrapping="Wrap" ToolTip="{text}" Text="{text}" />'
    )


def build_tags_xaml(mod: dict) -> str:
    cats = mod.get("categories") or []
    if not cats:
        return ""
    text = escape_xaml(" · ".join(cats))
    return (
        f'<TextBlock Margin="0,4,0,0" HorizontalAlignment="Center" TextAlignment="Center" '
        f'VerticalAlignment="Center" FontSize="13" '
        f'Foreground="{{DynamicResource ColorBrush4}}" TextWrapping="Wrap" ToolTip="{text}" Text="{text}" />'
    )


def build_desc_xaml(mod: dict) -> str:
    desc = (mod.get("description") or "").strip().replace("\n", " ")
    if not desc:
        return ""
    if len(desc) > 80:
        desc = desc[:80].rstrip() + "…"
    text = escape_xaml(desc)
    return (
        f'<TextBlock Margin="0,6,0,0" HorizontalAlignment="Center" TextAlignment="Center" '
        f'VerticalAlignment="Center" FontSize="12" Foreground="{{DynamicResource ColorBrush2}}" '
        f'TextWrapping="Wrap" MaxHeight="38" Text="{text}" />'
    )


def build_buttons_xaml(mod: dict) -> str:
    parts = []
    dl = mod.get("download_url") or ""
    if dl:
        parts.append(
            f'<local:MyIconTextButton Height="30" Margin="0,0,3,0" '
            f'Text="下载" EventType="下载文件" EventData="{escape_xaml(dl)}" '
            f'LogoScale="1" Logo="M19,9h-4V3H9v6H5l7,7L19,9zM5,18v2h14v-2H5z" />'
        )
    parts.append(
        f'<local:MyIconTextButton Height="30" Margin="{3 if dl else 0},0,0,0" '
        f'Text="查看详情" EventType="打开网页" EventData="{escape_xaml(mod["url"])}" '
        f'LogoScale="1" Logo="M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z" />'
    )
    return (
        '<StackPanel Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,8,0,0">\n'
        + "\n".join(parts)
        + "\n</StackPanel>"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="生成 PCL2 模组推荐主页 ModPage.xaml (Modrinth API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=None, help=f"每页模组数量 (默认 {modrinth_api.LIMIT})")
    p.add_argument("--columns", type=int, default=COLUMNS, help=f"列数 (默认 {COLUMNS})")
    p.add_argument("--use-cdn", action="store_true",
                   help="图片直接引用 Modrinth CDN webp(默认下载到 images/ 自托管)")
    p.add_argument("--dry-run", action="store_true", help="只打印卡片数据, 不生成文件/不写历史")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    limit = args.limit or modrinth_api.LIMIT

    mods = modrinth_api.get_mods(limit=limit, use_cdn=args.use_cdn,
                                 record_history=not args.dry_run)

    if args.dry_run:
        for i, m in enumerate(mods, 1):
            print(f"[{i}] {m['title']}  (mod/{m['slug']})")
            print(f"     作者: {m['author']}  下载: {fmt_count(m['downloads'])}  关注: {fmt_count(m['follows'])}")
            print(f"     分类: {' · '.join(m['categories']) or '-'}")
            print(f"     版本: MC {m.get('version') or '-'}")
            print(f"     图片: {m['img']}")
            print(f"     下载: {m.get('download_url') or '(无)'}")
            print(f"     链接: {m['url']}")
        print(f"\n[dry-run] 共 {len(mods)} 个模组, 未生成文件、未写历史。")
        return 0

    if not mods:
        print("[错误] 未能获取到模组数据，请检查网络或重试。")
        return 1

    def read_tpl(name: str) -> str:
        with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as f:
            return f.read()

    header = read_tpl("header.xaml")
    label_tpl = read_tpl("label.xaml")
    mod_tpl = read_tpl("mod.xaml")
    footer = read_tpl("footer.xaml")

    label_xaml = replaces(label_tpl, {"label": LABEL})

    rows = (len(mods) + args.columns - 1) // args.columns
    grid_columns = "".join(f'<ColumnDefinition Width="1*" />\n        ' for _ in range(args.columns))
    grid_rows = "".join(f'<RowDefinition Height="Auto" />\n        ' for _ in range(rows))

    items = []
    for index, m in enumerate(mods):
        stats = build_stats_xaml(m)
        tags = build_tags_xaml(m)
        desc = build_desc_xaml(m)
        buttons = build_buttons_xaml(m)
        data = {
            "row": index // args.columns,
            "column": index % args.columns,
            "img": m["img"],
            "name": m["title"],
            "stats": stats,
            "tags": tags,
            "desc": desc,
            "buttons": buttons,
            "url": m["url"],
        }
        items.append(replaces(mod_tpl, data, no_escape_keys=["stats", "tags", "desc", "buttons"]))

    games_block = "\n    ".join(items)
    games_grid = (
        f"    <Grid>\n"
        f"        <Grid.RowDefinitions>\n"
        f"        {grid_rows}</Grid.RowDefinitions>\n"
        f"        <Grid.ColumnDefinitions>\n"
        f"        {grid_columns}</Grid.ColumnDefinitions>\n"
        f"        {games_block}\n"
        f"    </Grid>"
    )

    final_xaml = header + "\n" + label_xaml + "\n" + games_grid + "\n" + footer
    with open("ModPage.xaml", "w", encoding="utf-8") as f:
        f.write(final_xaml)

    with open("ModPage.xaml.ini", "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))

    print(f"[成功] 已生成 ModPage.xaml, 共 {len(mods)} 个模组, {rows} 行 {args.columns} 列。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
