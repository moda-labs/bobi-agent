"""E2E: drive the bobbi setup UI in a real browser.

One screen: an objective-guided conversation (left) while the team materializes
as cards (right); special setup (Venn, native tokens, Slack) opens popups. The
Finish button appears only once all five things are gathered — goal, roles,
automations, connections, chat.
"""

from playwright.sync_api import expect

GOAL_MSG = "triage our github issues and route to the right engineer"


def _enter(page, url):
    """Pass through the intro (create-new, default location) into the editor."""
    page.goto(url)
    page.click("#introstart")
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)


def test_shows_chat_and_team_panel(page, bobbi_url):
    _enter(page, bobbi_url)
    expect(page.locator(".uni-chat #chinput")).to_be_visible()
    expect(page.locator(".uni-panel .up-title")).to_have_text("Your team")
    # Five cards: goal, roles, automations, connections, chat.
    expect(page.locator(".ucard")).to_have_count(5)
    expect(page.locator("#uni-meter")).to_have_text("0/5 gathered")
    # Finish is gated — not shown yet.
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)


def test_cards_materialize_and_chips_are_contextual(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    goal = page.locator(".ucard").first
    expect(goal).to_contain_text("Triage", timeout=10_000)
    expect(goal.locator(".udot.ok")).to_be_visible()
    # Suggestions from the brain become quick-add chips.
    expect(page.locator(".chip")).to_contain_text(["Also post a daily digest"])
    # github connection materialized in the Connections card.
    expect(page.locator(".uconn")).to_contain_text("GitHub")


def test_finish_appears_only_when_everything_gathered(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG)               # goal + roles + services
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("3/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)

    page.fill("#chinput", "yes, automatically flag stale PRs")   # automations
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("4/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)

    page.fill("#chinput", "I'll just use the command line")      # chat
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("5/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_be_visible()


def test_finish_builds_to_file_browser(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG + ", automatically flag stale PRs, via the command line")
    page.click("#chsend")
    finish = page.locator("#uni-foot [data-go='build']")
    expect(finish).to_be_visible(timeout=10_000)
    finish.click()
    # The post-build screen IS the built-in file browser: success banner + the
    # generated files read live from disk, with Open-folder and Finish actions.
    expect(page.locator(".filesdone")).to_be_visible(timeout=20_000)
    expect(page.locator(".fd-head h1")).to_contain_text("is ready")
    expect(page.locator("#fd-reveal")).to_be_visible()
    expect(page.locator("#fd-finish")).to_be_visible()
    # the team's files appear in the tree and open in the viewer
    expect(page.locator("#fd-tree .tnode", has_text="agent.yaml")).to_be_visible()
    page.locator("#fd-tree .tnode", has_text="agent.yaml").click()
    expect(page.locator("#fd-code")).to_contain_text("agent:")
    # Finish lands on a static completion screen (the local server has stopped,
    # so no server-dependent buttons are left to strand the user).
    page.click("#fd-finish")
    expect(page.locator(".done-wrap")).to_be_visible(timeout=10_000)
    expect(page.get_by_text("you can close this tab")).to_be_visible()


def test_native_secret_popup_captures_token(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    expect(page.locator(".uconn")).to_contain_text("GitHub", timeout=10_000)

    # A native connection opens its own setup popup, out of the chat.
    page.locator(".uconn", has_text="GitHub").locator("[data-secretopen]").click()
    ov = page.locator("#secret-ov")
    expect(ov).to_be_visible()
    expect(ov.locator(".mtab")).to_have_count(2)         # token | app
    expect(ov.locator(".steps li").first).to_be_visible()
    ov.locator("input[data-secret='GITHUB_TOKEN']").fill("ghp_" + "b" * 36)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)          # closes on connected
    expect(page.locator("body")).not_to_contain_text("ghp_bbbb")
    # The Connections card now shows github connected.
    expect(page.locator(".uconn", has_text="GitHub")).to_contain_text("connected")


def test_venn_services_share_one_unified_setup(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", "read my email and calendar and triage what matters")
    page.click("#chsend")
    # email + calendar are grouped under ONE Venn entry, not two "Connect"s.
    expect(page.locator(".uvenn")).to_be_visible(timeout=10_000)
    expect(page.locator(".uvenn .uconn.sub")).to_have_count(2)   # email, calendar
    expect(page.locator(".uvenn [data-vennsetup]")).to_have_count(1)

    page.locator(".uvenn [data-vennsetup]").click()
    ov = page.locator("#venn-ov")
    expect(ov).to_be_visible()
    expect(ov.locator(".venn-svcs .uconn")).to_have_count(2)     # both listed
    expect(ov.locator("#venn-key")).to_be_visible()             # one shared key
    expect(ov.locator(".steps li").first).to_be_visible()


def test_chat_card_select_and_slack_setup(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    chat = page.locator(".ucard", has_text="Chat")
    expect(chat).to_be_visible(timeout=10_000)
    # Picking Slack reveals its setup popup affordance.
    chat.locator("[data-chatset='slack']").click()
    slack_btn = chat.locator("[data-secretopen='slack']")
    expect(slack_btn).to_be_visible()
    slack_btn.click()
    ov = page.locator("#secret-ov")
    expect(ov).to_be_visible()
    expect(ov.locator(".steps li").first).to_contain_text("api.slack.com")
    expect(ov.locator("input[data-secret='SLACK_BOT_TOKEN']")).to_be_visible()

    # Capture the bot token; the chat card then shows it saved with Copy/Edit.
    ov.locator("input[data-secret='SLACK_BOT_TOKEN']").fill("xoxb-" + "1" * 24)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)
    saved = page.locator(".ucard", has_text="Chat").locator(".secret-saved")
    expect(saved).to_contain_text("Slack bot token saved")
    expect(saved.locator("[data-secretcopy='SLACK_BOT_TOKEN']")).to_be_visible()


def test_saved_key_can_be_re_edited(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    # Connect github, then the row offers an "edit" affordance to re-enter it.
    page.locator(".uconn", has_text="GitHub").locator("[data-secretopen]").click()
    ov = page.locator("#secret-ov")
    ov.locator("input[data-secret='GITHUB_TOKEN']").fill("ghp_" + "d" * 36)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)

    page.locator(".uconn", has_text="GitHub").locator("[data-secretopen]").click()
    ov2 = page.locator("#secret-ov")
    expect(ov2.locator(".secret-saved")).to_contain_text("saved")
    ov2.locator("[data-secretedit='GITHUB_TOKEN']").click()
    expect(ov2.locator("input[data-secret='GITHUB_TOKEN']")).to_be_visible()


def test_pasted_secret_is_redacted_in_transcript(page, bobbi_url):
    _enter(page, bobbi_url)
    page.fill("#chinput", "my github token is ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    page.click("#chsend")
    expect(page.locator(".msg.you").last).to_contain_text("[redacted]", timeout=10_000)
    expect(page.locator(".msg.you").last).not_to_contain_text("ghp_aaaa")


def test_intro_offers_three_ways_in_and_starts_editor(page, bobbi_url):
    page.goto(bobbi_url)
    expect(page.get_by_role("heading", name="Build an agent team")).to_be_visible()
    # Three ways in: create, modify existing, from a registry.
    expect(page.locator("[data-intromode='create']")).to_be_visible()
    expect(page.locator("[data-intromode='registry']")).to_be_visible()
    # No local teams in a fresh project → Modify-existing is disabled.
    expect(page.locator("[data-intromode='open']")).to_be_disabled()
    # Create has no name field — the team is auto-named in the chat. Location
    # defaults to the bobbi/ working folder.
    expect(page.locator("#introname")).to_have_count(0)
    expect(page.locator("#introloc")).to_have_value("bobbi/")
    page.click("#introstart")
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)
    expect(page.locator(".uni-panel .up-title")).to_have_text("Your team")


def test_folder_picker_browses_and_fills_location(page, bobbi_url):
    page.goto(bobbi_url)
    expect(page.locator("#introloc")).to_be_visible()
    page.click("#introbrowse")
    expect(page.locator(".picker")).to_be_visible()
    # The project root lists its folders; drilling into one fills the field.
    target = page.locator(".pnode:not(.up)").first
    expect(target).to_be_visible()
    name = target.inner_text().replace("📁", "").strip()
    target.click()
    page.click("#pick-use")
    expect(page.locator(".picker")).to_have_count(0)
    expect(page.locator("#introloc")).to_have_value(name)
