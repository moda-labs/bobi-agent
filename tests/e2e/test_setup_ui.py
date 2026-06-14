"""E2E: drive the bobbi setup UI in a real browser through the create flow."""

from playwright.sync_api import expect


def test_rail_shows_five_perceived_steps(page, bobbi_url):
    page.goto(bobbi_url)
    expect(page.locator(".step")).to_have_count(5)
    expect(page.locator("#counter")).to_have_text("01 / 05")
    expect(page.locator(".step")).to_contain_text(
        ["Design", "Automate", "Connect", "Chat", "Done"])


def test_design_reflects_and_offers_to_continue(page, bobbi_url):
    page.goto(bobbi_url)
    page.fill("#chinput", "triage our github issues and route to the right engineer")
    page.click("#chsend")
    # bob's reflection streams into a bubble...
    expect(page.locator(".msg.bob").last).to_contain_text("triage", timeout=10_000)
    # ...and once the goal is clear, the inline continue button appears.
    expect(page.locator("#readygo button")).to_be_visible()


def test_full_create_flow_to_done_and_inspect(page, bobbi_url):
    page.goto(bobbi_url)
    page.fill("#chinput", "triage our github issues and route to the right engineer")
    page.click("#chsend")
    expect(page.locator("#readygo button")).to_be_visible(timeout=10_000)

    page.click("#readygo button")                       # Design → Automate
    expect(page.locator("#autonext")).to_be_visible(timeout=10_000)
    page.click("#autonext")                             # Automate → Connect
    page.get_by_role("button", name="Next: Chat →").click()   # Connect → Chat
    expect(page.get_by_role("heading", name="How will you talk to your team?")
           ).to_be_visible(timeout=10_000)
    page.get_by_role("button", name="Build my team →").click()  # collapsed build

    # The build/validate/install run behind one action and land on Done.
    expect(page.get_by_role("heading", name="team bobbi is ready")
           ).to_be_visible(timeout=20_000)
    expect(page.get_by_text("bobbi start")).to_be_visible()

    # The optional file inspector lists the authored pack.
    page.get_by_role("button", name="View files").click()
    expect(page.locator("[data-ifile]")).to_have_count(5)
    expect(page.locator("[data-ifile='agent.yaml']")).to_be_visible()
    expect(page.locator("[data-ifile='roles/triager/ROLE.md']")).to_be_visible()


def test_chat_step_offers_channels(page, bobbi_url):
    page.goto(bobbi_url)
    page.fill("#chinput", "triage github issues")
    page.click("#chsend")
    expect(page.locator("#readygo button")).to_be_visible(timeout=10_000)
    page.click("#readygo button")
    page.click("#autonext")
    page.get_by_role("button", name="Next: Chat →").click()
    # Command line (default), Slack; picking Slack reveals its token field.
    expect(page.locator("[data-channel='cli']")).to_be_visible()
    page.click("[data-channel='slack']")
    expect(page.locator("#chsetup-slack input")).to_be_visible()


def test_pasted_secret_is_redacted_in_transcript(page, bobbi_url):
    page.goto(bobbi_url)
    page.fill("#chinput", "my github token is ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    page.click("#chsend")
    # After the turn, the persisted user bubble shows the scrubbed value.
    expect(page.locator(".msg.you").last).to_contain_text("[redacted]", timeout=10_000)
    expect(page.locator(".msg.you").last).not_to_contain_text("ghp_aaaa")
