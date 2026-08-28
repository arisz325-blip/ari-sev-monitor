# SEVs Import Watch

Daily watch on the **Specialist and Enthusiast Vehicles (SEVs) Register** —
the Australian list of model variants that may be imported through a Registered
Automotive Workshop. When the Department of Infrastructure adds a car to the
register, your phone gets a push; the whole register is browsable on a
dashboard, filterable by brand.

It also tracks the **Model Report register**, which is the difference between
"allowed in principle" and "someone can actually build it":

| | Meaning |
|---|---|
| On the SEVs list | the variant *may* be imported through a RAW |
| …**and** has an in-force model report | a workshop is set up to comply it — the dashboard names which one |
| …with **no** model report | nobody can bring one in yet, however good the listing looks |

- Source: [SEVs Register](https://www.rover.infrastructure.gov.au/PublishedApprovals/SEVApprovals)
  and [Model Reports](https://www.rover.infrastructure.gov.au/PublishedApprovals/MREApprovals) on ROVER
- Push: [ntfy.sh](https://ntfy.sh) → native Android/iOS notification
- Dashboard: GitHub Pages, reads `docs/data.json`
- Cost: nothing. Runs on the free GitHub Actions cron.

```
checker.py                     scraper + differ + notifier
config.json                    all tuning: notify toggles, watch filters, budgets
state.json                     generated — last seen register, the diff baseline
docs/index.html                dashboard
docs/data.json                 generated — what the dashboard reads
.github/workflows/check.yml    daily cron; writes diagnostics to the run Summary
.github/workflows/tests.yml    offline suite on every push
tests/                         100 checks against a mock ROVER portal, no network
```

## What it tells you

| Event | Meaning | Pushed by default |
|---|---|---|
| `new` | a model variant was added — it is now importable | yes |
| `report_added` | a workshop's model report went in force — the car can actually be built now | yes |
| `report_lost` | the last in-force model report for a car went away | yes |
| `returned` | an expired or removed entry is back in force | yes |
| `under_review` | the department flagged an entry; it may be varied or removed | yes |
| `removed` | an entry vanished from the register | yes |
| `expired` | an entry's approval lapsed | no |
| `review_cleared` | a review flag was lifted | no |
| `expiry_changed` | the entry's expiry date moved | no |
| `updated` | make / model / category / model code / build dates edited | no |

Everything is recorded on the dashboard regardless; the toggles in
`config.json → notify` only decide what interrupts you.

## Setup

1. **Push the repo to GitHub** (public — GitHub Pages needs it on the free tier).

2. **Pick an ntfy topic.** Any hard-to-guess string, e.g.
   `sev-import-a7f3k9`. Anyone who knows it can read your notifications, so
   treat it like a password.
   - Install the ntfy app (Android/iOS), subscribe to that topic.
   - In the repo: Settings → Secrets and variables → Actions → New secret,
     name `NTFY_TOPIC`, value your topic.
   - Optional secrets: `NTFY_SERVER` (self-hosted server), `NTFY_TOKEN`
     (bearer token for a protected topic).

3. **Turn on Pages.** Settings → Pages → Source: *Deploy from a branch* →
   `main` / `/docs`. The dashboard lands at
   `https://<user>.github.io/<repo>/`.

4. **Seed the baseline.** Actions → *SEVs Register check* → Run workflow →
   `normal`. The first run records all ~1100 entries and deliberately notifies
   nothing — otherwise it would fire a thousand pushes. Every run after that
   only reports what changed.

5. **Read the pages the grid cannot give you.** Two things exist only on an
   approval's own page: which SEV entries a model report covers, and which
   *variant* an entry is (HM35 vs PNM35). That is ~950 report pages and ~1100
   entry pages, once. Locally, `python checker.py --backfill` does the lot
   (~70 min); otherwise each scheduled run works through another 60 of each and
   settles itself within a few weeks. Newly published reports and brand-new
   entries always jump the queue, so notifications are never delayed by the
   backfill.

6. **Check the push works.** Run the workflow again with mode `notify-test`.

The scheduled run is 22:00 UTC daily — 8am Melbourne in winter, 9am during
daylight saving. Change the `cron:` line in `.github/workflows/check.yml` to
move it, or add a second entry to check twice a day.

## Local use

```bash
pip install -r requirements.txt
python checker.py --dry-run      # check + report, send nothing, save nothing
python checker.py --self-test    # is the portal still shaped as we expect?
python checker.py --notify-test  # one test push (needs NTFY_TOPIC)
python checker.py --rebuild      # discard the baseline and re-seed it silently
python checker.py --backfill     # read every outstanding detail page at once
python -m tests.test_checker     # offline suite, no network
```

A full run makes ~25 requests and takes about a minute, plus one page fetch per
model report still waiting to be linked.

## Tuning `config.json`

**Only tell me about certain cars.** Leave the filters empty and everything is
pushed (that is the default — the register only grows by a handful of entries a
week). To narrow it:

```json
"watch": {
  "notify_watched_only": true,
  "makes": ["nissan", "toyota"],
  "keywords": ["skyline", "gt-r", "bnr32"],
  "categories": ["lc - motor cycle"]
}
```

An entry matching *any* of the three lists is pushed. The dashboard still shows
the whole register.

**Other knobs**

| Key | Does |
|---|---|
| `notify.max_per_run` | cap on pushes per run; the last one says how many were held back |
| `notify.group_same_model` | one push per model instead of one per variant (default on) |
| `details.budget_per_run` | entry detail pages read per run while backfilling variants |
| `details.max_new_per_run` | never-seen entries read ahead of the backfill queue |
| `details.include_expired` | read detail pages for expired entries too |
| `sanity.max_shrink_ratio` | how much the register may shrink in one run before disappearances are treated as a portal glitch rather than removals |
| `dashboard.recent_days` | how long an entry counts as "new" |
| `dashboard.expiring_soon_days` | how far ahead the "Expiring" tile looks |
| `source.page_size` | rows per grid request (100 ≈ 26 MB of JSON each) |
| `model_reports.link_budget_per_run` | model report pages read per run while backfilling |
| `model_reports.enabled` | set false to skip the model report register entirely |

## Using the dashboard

Filter row one picks the slice (New / In force / Expiring / Expired / All).
Row two is the **brand picker** — every make in the register with how many
entries it has and how many of those have a model report — the **model report
filter** (any / has one / has none), and the **Group variants** toggle. The
search box also matches model codes, criteria, MRE numbers and workshop names,
so `sydney` finds everything one workshop can build.

**One entry is one variant, not one car.** Five register entries reading
"NISSAN Stagea · M35 SERIES" are HM35 300RX, M35 Axis 350S, NM35 250T, PM35
350RX and PNM35 350RX FOUR — the grid's model code is the *series*, and the
chassis code is only on each entry's own page. With grouping on (the default)
those collapse into one card, "NISSAN Stagea · 5 variants · 4 of 5 have a model
report"; open it and each variant is listed with its engine, its SEV number and
its own report badge. Grouping is by make + model + model code, so generations
stay apart: Skyline R series, V35, V36, J50 and RV37 remain five cards. In-force
goes from 585 rows to 479. Turn the toggle off for the flat one-row-per-SEV
list.

Each card shows the build date range, category, model code, the variant
condition where there is one, and either a green **Model report** badge naming
the workshop that holds it or an amber **No model report** badge. Tapping a
card (or a variant inside a group) opens that entry on ROVER.

## Reading the register

An entry means *this model variant may be imported via a RAW*. It is not an
import approval by itself — you still apply for one, and the SEVs pathway is
only one of several (vehicles over 25 years old, personal imports and pre-1989
vehicles have their own). Each entry carries the criterion it was approved
under (performance, environmental, rarity, mobility, left-hand drive…), a build
date range, a model code, and often a variant condition that narrows it
further — "MUST be JG5 Model Code BEV ONLY" excludes every other N-ONE. Read
the entry on ROVER before spending money.

Entries expire (typically a few years out) and can be put under review. Both
show on the dashboard; expiry pushes are off by default because they are
routine and rarely urgent.

---

## 中文速记

这是澳洲 SEVs 名录（可通过 RAW 渠道进口的车型清单）的每日监控。

- 数据来自 ROVER 官网的 SEVs Register，每天跑一次，新增车型用 ntfy 推到手机。
- **名单上有 ≠ 现在就能进**：还要有工作坊持有 in-force 的 **Model Report**，才真的有人能把车合规化。看板上绿色徽章 = 有 model report（并写明是哪家 RAW），黄色 = 名单上有但没人做。有新的 model report 生效也会推送。
- 网页看板部署在 GitHub Pages，手机上可搜索、按**品牌**筛选、按有无 model report 筛选，还有 新增 / 有效 / 即将到期 / 已过期 的分类。
- 部署三步：把仓库推到 GitHub → 在仓库 Secrets 里加 `NTFY_TOPIC`（手机 ntfy App 订阅同一个 topic）→ Settings → Pages 选 `main` 分支的 `/docs` 目录。
- 第一次运行只建立基线、不推送（否则会一次推一千条）；之后每天只推变化。
- **同车型多条 SEV 编号不是重复**，是不同变体：列表里的 model code 是「系列」（M35 SERIES），真正的底盘代号（HM35 / PNM35）只在每条自己的详情页上。看板默认按 品牌+车型+model code 合并成一张卡（585 条变 479 张），点开列出每个变体的底盘代号、发动机、SEV 编号和各自的 report 状态；右上角「Group variants」可以关掉看平铺列表。
- 同一次运行里同一车型的多个新增会**合成一条推送**（「NISSAN Stagea ×5」并列出各变体），不会连推 5 条；`config.json` 里 `notify.group_same_model` 设 false 可恢复逐条推送。
- Model report 的归属、以及条目的变体信息，都只写在各自的详情页上（约 950 + 1100 页），本地跑一次 `python checker.py --backfill`（约 70 分钟）可以一次补完，不跑也会每天各补 60 份自动收敛；新发布的 report 和新条目永远排在队首，不会因为补数据而延迟推送。
- 只想收特定品牌的推送，改 `config.json` 的 `watch`：把 `notify_watched_only` 设为 `true`，在 `makes` / `keywords` 里填关键词。
- 名录上有条目 ≠ 已获批进口，仍需另行申请；买车前请以 ROVER 官网条目为准。
