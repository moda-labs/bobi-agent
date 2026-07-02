"""E2E: drive the bobi setup UI in a real browser.

One screen: an objective-guided conversation (left) while the team materializes
as cards (right); special setup (Venn, native tokens, Slack) opens popups. The
Finish button appears only once all five things are gathered — goal, roles,
automations, connections, chat.
"""

from playwright.sync_api import expect

GOAL_MSG = "triage our github issues and route to the right engineer"


def _write_team_source(src, name):
    """Write a minimal valid team source at src."""
    (src / "roles" / "lead").mkdir(parents=True)
    (src / "agent.yaml").write_text(
        "agent: " + name + "\nversion: 0.1.0\nentry_point: lead\n"
        "services:\n  - name: github\n    events: true\nchat: slack\n")
    (src / "agent.md").write_text("# " + name + "\n\nWatch the repo.\n")
    (src / "roles" / "lead" / "ROLE.md").write_text("# Lead\n\nRoute issues.\n")
    return src


def _seed_library_team(home, name="legacy-bot"):
    """Write a minimal valid team source into the BOBI_HOME/agents library."""
    return _write_team_source(home / "agents" / name / "src", name)


def _enter(page, url):
    """Welcome on-ramp → intro → start a new team from scratch → the editor."""
    page.goto(url)
    page.click("#welcome-go")                      # "Get started" on the welcome
    page.click("[data-newteam]")                   # "Customize my own agent team"
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)


def test_shows_chat_and_team_panel(page, bobi_url):
    _enter(page, bobi_url)
    expect(page.locator(".uni-chat #chinput")).to_be_visible()
    expect(page.locator(".uni-panel .up-title")).to_have_text("Your team")
    # Five cards: goal, roles, automations, connections, chat.
    expect(page.locator(".ucard")).to_have_count(5)
    expect(page.locator("#uni-meter")).to_have_text("0/5 gathered")
    # Finish is gated — not shown yet.
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)


def test_cards_materialize_after_goal(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    goal = page.locator(".ucard").first
    expect(goal).to_contain_text("Triage", timeout=10_000)
    expect(goal.locator(".udot.ok")).to_be_visible()
    # github connection materialized in the Connections card (its own row,
    # distinct from the account-level Venn row).
    expect(page.locator(".uconn", has_text="GitHub")).to_be_visible()


def test_finish_appears_only_when_everything_gathered(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", GOAL_MSG)               # goal + roles (services need connection)
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("2/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)

    page.fill("#chinput", "yes, automatically flag stale PRs")   # automations
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("3/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)

    # Connect the implied GitHub service so connections count as gathered.
    github_row = page.locator(".uconn", has_text="GitHub")
    expect(github_row).to_be_visible(timeout=5_000)
    github_row.locator("[data-secretopen]").click()
    ov = page.locator("#secret-ov")
    ov.locator("input[data-secret='GITHUB_TOKEN']").fill("ghp_" + "t" * 36)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)
    expect(page.locator("#uni-meter")).to_have_text("4/5 gathered", timeout=5_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_have_count(0)

    page.fill("#chinput", "I'll just use the command line")      # chat
    page.click("#chsend")
    expect(page.locator("#uni-meter")).to_have_text("5/5 gathered", timeout=10_000)
    expect(page.locator("#uni-foot [data-go='build']")).to_be_visible()


def test_finish_builds_to_file_browser(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", GOAL_MSG + ", automatically flag stale PRs, via the command line")
    page.click("#chsend")
    # Wait for brain to process, then connect the implied GitHub service so the
    # Connections slot counts as gathered (connections require actual connection).
    github_row = page.locator(".uconn", has_text="GitHub")
    expect(github_row).to_be_visible(timeout=10_000)
    github_row.locator("[data-secretopen]").click()
    ov = page.locator("#secret-ov")
    ov.locator("input[data-secret='GITHUB_TOKEN']").fill("ghp_" + "t" * 36)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)
    finish = page.locator("#uni-foot [data-go='build']")
    expect(finish).to_be_visible(timeout=10_000)
    finish.click()
    # The post-build screen is a read-only Preview: the generated files read
    # live from disk, with Open-folder and Finish actions.
    expect(page.locator(".filesdone")).to_be_visible(timeout=20_000)
    expect(page.locator(".fd-head .eyebrow")).to_contain_text("Preview")
    expect(page.locator("#fd-reveal")).to_be_visible()
    expect(page.locator("#fd-finish")).to_be_visible()
    # the team's files appear in the tree and open in the viewer
    expect(page.locator("#fd-tree .tnode", has_text="agent.yaml")).to_be_visible()
    page.locator("#fd-tree .tnode", has_text="agent.yaml").click()
    expect(page.locator("#fd-code")).to_contain_text("agent:")
    # Finish lands on the completion screen offering two deployment paths —
    # local (`bobi agent <name> start`) and cloud (the Fly provisioner) — plus a Done
    # button into the team hub (the server stays alive — it's re-entrant now).
    page.click("#fd-finish")
    expect(page.locator(".done-wrap")).to_be_visible(timeout=10_000)
    expect(page.locator(".deploy-opt", has_text="Local")).to_contain_text(
        "bobi agent")
    expect(page.locator(".deploy-opt", has_text="Cloud")).to_contain_text(
        "provision-instance.sh")
    expect(page.locator("#done-home")).to_be_visible()
    # Done goes to the team hub, where the freshly built team is listed.
    page.click("#done-home")
    expect(page.locator(".home-grid")).to_be_visible(timeout=10_000)


def test_escape_closes_connection_overlay(page, bobi_url):
    # Escape closes the connection-setup popup too (not just the folder picker).
    _enter(page, bobi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    expect(page.locator(".uconn", has_text="GitHub")).to_be_visible(timeout=10_000)
    page.locator(".uconn", has_text="GitHub").locator("[data-secretopen]").click()
    expect(page.locator("#secret-ov")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#secret-ov")).to_have_count(0)


def test_native_secret_popup_captures_token(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", GOAL_MSG)
    page.click("#chsend")
    expect(page.locator(".uconn", has_text="GitHub")).to_be_visible(timeout=10_000)

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


def test_venn_is_an_account_connection_with_per_service_rows(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", "read my email and calendar and triage what matters")
    page.click("#chsend")
    # Venn-backed services are their OWN rows (tagged "via Venn"), not grouped.
    expect(page.locator(".uconn", has_text="Email")).to_be_visible(timeout=10_000)
    expect(page.locator(".uconn", has_text="Calendar")).to_be_visible()
    # Venn itself is ONE account-level row, with a single set-up link.
    venn_row = page.locator(".uconn.venn-acct")
    expect(venn_row).to_be_visible()
    expect(venn_row).to_contain_text("Venn")
    expect(page.locator("[data-vennsetup]")).to_have_count(1)

    # The setup modal opens at the key-entry step: paste a key, then connect.
    page.locator("[data-vennsetup]").click()
    ov = page.locator("#venn-ov")
    expect(ov).to_be_visible()
    expect(ov.locator("#venn-key")).to_be_visible()             # paste the key
    expect(ov.locator("[data-vennconnect]")).to_be_visible()    # connect button
    expect(ov.locator(".steps li").first).to_be_visible()


def test_chat_card_select_and_slack_setup(page, bobi_url):
    _enter(page, bobi_url)
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
    expect(ov.locator(".steps li").first).to_contain_text("create-slack-bot")
    expect(ov.locator("input[data-secret='SLACK_BOT_TOKEN']")).to_be_visible()

    # Capture the bot token; the chat card then shows it saved with Copy/Edit.
    ov.locator("input[data-secret='SLACK_BOT_TOKEN']").fill("xoxb-" + "1" * 24)
    ov.get_by_role("button", name="Connect").click()
    expect(ov).to_have_count(0, timeout=10_000)
    saved = page.locator(".ucard", has_text="Chat").locator(".secret-saved")
    expect(saved).to_contain_text("Slack bot token saved")
    expect(saved.locator("[data-secretcopy='SLACK_BOT_TOKEN']")).to_be_visible()


def test_saved_key_can_be_re_edited(page, bobi_url):
    _enter(page, bobi_url)
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


def test_pasted_secret_is_redacted_in_transcript(page, bobi_url):
    _enter(page, bobi_url)
    page.fill("#chinput", "my github token is ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    page.click("#chsend")
    expect(page.locator(".msg.you").last).to_contain_text("[redacted]", timeout=10_000)
    expect(page.locator(".msg.you").last).not_to_contain_text("ghp_aaaa")


def test_welcome_leads_to_intro_with_custom_and_starts_editor(page, bobi_url):
    # New users land on the welcome on-ramp first.
    page.goto(bobi_url)
    expect(page.get_by_role(
        "heading", name="Build a team of agents that runs your work")).to_be_visible()
    page.click("#welcome-go")
    # The intro: a prominent "customize my own" option plus template rows.
    expect(page.get_by_role("heading", name="Build an agent team")).to_be_visible()
    expect(page.locator("[data-newteam]")).to_be_visible()
    # No name field, no inline location input — location is a quiet FYI line.
    expect(page.locator("#introname")).to_have_count(0)
    expect(page.locator("#loc-path")).to_be_visible()
    page.click("[data-newteam]")
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)
    expect(page.locator(".uni-panel .up-title")).to_have_text("Your team")


def test_template_lands_in_library_slot_and_shows_on_hub(page, bobi, monkeypatch):
    # The eng-team-template bug: picking a template used to bury its source at
    # agents/new-agent/src/<name>, one level deeper than the hub scan reads, so
    # the finished team never appeared on the home screen. Clicking a template
    # row must land it in its own library slot and the hub must list it.
    # The registry is faked in-process: the server thread shares this
    # interpreter, so monkeypatching open_mode is visible to it.
    from bobi.setup import open_mode

    def fake_list_registry_teams(proj):
        return [{"name": "eng-team", "description": "An engineering team.",
                 "official": True, "registry": "test"}]

    def fake_fetch_into(proj, name, dest):
        stage = _write_team_source(bobi.home / "stage" / name, name)
        open_mode.copy_into(stage, dest)

    monkeypatch.setattr(open_mode, "list_registry_teams", fake_list_registry_teams)
    monkeypatch.setattr(open_mode, "fetch_into", fake_fetch_into)

    page.goto(bobi.url)
    page.click("#welcome-go")
    row = page.locator("[data-template='eng-team']")
    expect(row).to_be_visible(timeout=5_000)       # template list loads lazily
    row.click()
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)  # in the editor
    # The source landed in the template's own library slot.
    slot = bobi.home / "agents" / "eng-team" / "src"
    assert (slot / "agent.yaml").is_file()
    # And the hub lists it.
    page.click(".brand[data-home]")
    expect(page.locator(".hcard", has_text="eng-team")).to_be_visible()


def test_template_at_custom_location_lands_in_child_folder(page, bobi,
                                                           monkeypatch):
    # A user-picked folder is a container: the template lands in a child named
    # after it (and stays visible on the hub via the session's source_dir).
    from bobi.setup import open_mode

    def fake_list_registry_teams(proj):
        return [{"name": "eng-team", "description": "An engineering team.",
                 "official": True, "registry": "test"}]

    def fake_fetch_into(proj, name, dest):
        stage = _write_team_source(bobi.home / "stage" / name, name)
        open_mode.copy_into(stage, dest)

    monkeypatch.setattr(open_mode, "list_registry_teams", fake_list_registry_teams)
    monkeypatch.setattr(open_mode, "fetch_into", fake_fetch_into)

    page.goto(bobi.url)
    page.click("#welcome-go")
    # Change the location to home/projects via the picker.
    page.click("#loc-change")
    page.click(".pnode.up")
    page.locator(".pnode", has_text="projects").click()
    page.click("#pick-use")
    expect(page.locator("#loc-path")).to_have_text(str(bobi.home / "projects"))
    row = page.locator("[data-template='eng-team']")
    expect(row).to_be_visible(timeout=5_000)
    row.click()
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)
    assert (bobi.home / "projects" / "eng-team" / "agent.yaml").is_file()
    page.click(".brand[data-home]")
    expect(page.locator(".hcard", has_text="eng-team")).to_be_visible()


def test_change_location_picker_updates_fyi(page, bobi):
    page.goto(bobi.url)
    page.click("#welcome-go")
    expect(page.locator("#loc-path")).to_be_visible()
    page.click("#loc-change")
    expect(page.locator(".picker")).to_be_visible()
    # Opens in the (empty) library; step up to home, which has real folders.
    # Drilling into one updates the FYI line with that folder's absolute path.
    page.click(".pnode.up")
    expect(page.locator("#pick-path")).to_have_text(str(bobi.home))
    target_path = str(bobi.home / "projects")
    target = page.locator(".pnode", has_text="projects")
    expect(target).to_be_visible()
    target.click()
    page.click("#pick-use")
    expect(page.locator(".picker")).to_have_count(0)
    expect(page.locator("#loc-path")).to_have_text(target_path)


def test_escape_closes_popup(page, bobi_url):
    # Escape dismisses the topmost popup (here, the folder picker).
    page.goto(bobi_url)
    page.click("#welcome-go")
    page.click("#loc-change")
    expect(page.locator(".picker")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".picker")).to_have_count(0)


def test_homepage_lists_teams_and_opens_one(page, bobi):
    # With a team in the library, setup boots straight to the team hub; each
    # card shows the team's description, and clicking one opens it in the editor.
    _seed_library_team(bobi.home, "legacy-bot")
    page.goto(bobi.url)
    card = page.locator(".hcard", has_text="legacy-bot")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Watch the repo")        # description from agent.md
    card.click()
    # Lands in the editor with the existing team reverse-filled from source.
    expect(page.locator("#chinput")).to_be_visible(timeout=5_000)


def test_welcome_button_goes_to_homepage(page, bobi_url):
    # The welcome on-ramp offers a direct path to the team hub, so returning
    # users don't have to walk through setup to reach their teams.
    page.goto(bobi_url)
    expect(page.get_by_role(
        "heading", name="Build a team of agents that runs your work")).to_be_visible()
    page.click("#welcome-home")
    # Lands on the team hub (empty library → just the "add a team" card).
    expect(page.get_by_role("heading", name="Your agent teams")).to_be_visible()
    expect(page.locator("[data-addteam]")).to_be_visible()


def test_brand_icon_navigates_home_from_anywhere(page, bobi_url):
    # The titlebar brand is a home button reachable from any screen — here,
    # mid-flow on the intro, clicking it jumps straight to the team hub.
    page.goto(bobi_url)
    page.click("#welcome-go")
    expect(page.get_by_role("heading", name="Build an agent team")).to_be_visible()
    page.click(".brand[data-home]")
    expect(page.get_by_role("heading", name="Your agent teams")).to_be_visible()


def test_disconnect_overlay_when_server_dies(page, bobi):
    # The page is useless without its local server — if it dies, the UI must
    # say so and stop pretending to be live (heartbeat catches it within ~4s).
    page.goto(bobi.url)
    # Empty library → the welcome on-ramp is the first screen.
    expect(page.get_by_role(
        "heading", name="Build a team of agents that runs your work")).to_be_visible()
    expect(page.locator("#disc-ov")).to_have_count(0)   # live: no overlay
    bobi.stop()                                        # server gone
    expect(page.locator("#disc-ov")).to_be_visible(timeout=8_000)
    expect(page.get_by_role("heading", name="Setup server disconnected")
           ).to_be_visible()
