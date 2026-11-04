"""Computed WCAG contrast for the shared styles used by workspace controls (#81)."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.browser
WEB = Path(__file__).resolve().parents[1] / "src/asr_studio/web"

# Small specimens keep every affected selector measurable without hidden dialogs,
# disabled provider controls, or a live inference service.
SPECIMENS = """
<section style="background:var(--surface-subtle)">
  <input class="history-search" placeholder="Tìm cuộc họp">
  <select class="cal-month"><option>Tháng 10</option></select>
  <input class="cal-year" value="2026">
  <button class="cal-step">Trước</button><button class="cal-today">Hôm nay</button>
  <button class="cal-undated">Chưa đặt ngày</button><button class="cal-more">Xem thêm</button>
  <div class="calendar"><button class="cal-day is-outside"><span class="cal-number"
    data-text>30</span></button></div>
  <div class="calendar calendar-large"><button class="cal-day is-outside"><span
    class="cal-number" data-text>30</span></button></div>
  <span style="color:var(--ink-3)" data-text>Version · Thứ hai · Tiến độ</span>
  <a class="recent-link" aria-current="page"><span class="link-date" data-text>17/10</span></a>
  <a class="recent-link" id="hover-recent"><span class="link-date" data-text>18/10</span></a>
</section>
<div class="selected-file"><div class="selected-file-copy"><span data-text>10 MB</span></div></div>
<div class="transcription-language-control"><select><option>English</option></select></div>
<div class="generation-navigation"><select><option>Transcript</option></select></div>
<div class="speaker-add-controls"><input id="speaker-add-name" placeholder="Tên người nói"></div>
<form class="speaker-name-form"><input value="Lan"></form>
<select class="segment-speaker-select"><option>Lan</option></select>
<textarea class="segment-text-editor">Nội dung</textarea>
<div class="field-group"><input type="text" placeholder="Tên cuộc họp"><input type="date"></div>
<div class="meeting-delete-body"><input placeholder="Xác nhận xóa"></div>
<label class="language-option"><input type="radio">English</label>
<label class="language-option"><input type="radio" checked>Tiếng Việt</label>
<div class="notes-output-language-control"><select><option>English</option></select></div>
<div class="notes-revision-select-wrap"><select><option>Phiên bản 1</option></select></div>
<div class="connection-model-section"><select><option>Model</option></select></div>
<div class="notes-frame"><textarea>Viết notes</textarea></div>
<section class="settings-asr">
  <h3 class="settings-group-title" data-text>Các model chép lời</h3>
  <p class="asr-default" data-text>Mặc định theo phần cứng của máy: whisper-large-v3.</p>
  <ul class="asr-models">
    <li class="asr-model" data-state="ready"><div class="asr-model-head">
      <strong data-text>Whisper</strong>
      <span class="asr-badge" data-kind="default" data-text>Mặc định</span>
      <span class="asr-badge" data-kind="status" data-text>Sẵn sàng</span></div>
      <p data-text>Đa ngôn ngữ.</p></li>
    <li class="asr-model" data-state="not_prepared"><div class="asr-model-head">
      <strong data-text>Qwen</strong>
      <span class="asr-badge" data-kind="status" data-text>Chưa chuẩn bị</span></div>
      <p class="asr-model-guidance" data-text>Chạy lệnh sau trong thư mục ứng dụng.</p>
      <div class="asr-command"><label data-text>Lệnh chuẩn bị model</label>
        <div class="asr-command-field"><input readonly value="docker compose run"><button
          data-text>Sao chép</button></div>
        <span class="asr-command-result" data-text>Đã sao chép lệnh.</span></div></li>
    <li class="asr-model" data-state="gpu_required"><div class="asr-model-head">
      <strong data-text>Qwen</strong>
      <span class="asr-badge" data-kind="status" data-text>Cần GPU NVIDIA</span></div></li>
  </ul>
</section>
"""

MEASURE = r"""() => {
  const rgba = value => {
    const v = value.match(/[\d.]+/g).map(Number);
    return [...v.slice(0, 3), v[3] ?? 1];
  };
  const over = (a, b) => a.slice(0, 3).map((v, i) => v*a[3] + b[i]*(1-a[3])).concat(1);
  const background = el => {
    const chain = [];
    for (; el; el = el.parentElement) chain.unshift(el);
    return chain.reduce((bg, node) => over(rgba(getComputedStyle(node).backgroundColor), bg),
      [255, 255, 255, 1]);
  };
  const luminance = rgb => rgb.slice(0, 3).map(v => {
    v /= 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, v, i) => sum + v*[0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (a, b) => {
    const [hi, lo] = [luminance(over(a, b)), luminance(b)].sort((a, b) => b-a);
    return (hi + 0.05) / (lo + 0.05);
  };
  const results = [];
  for (const el of document.querySelectorAll('[data-text], [placeholder]')) {
    const pseudo = el.hasAttribute('placeholder') ? '::placeholder' : null;
    const style = getComputedStyle(el, pseudo);
    const color = rgba(style.color);
    color[3] *= Number(style.opacity);
    results.push({element: el.outerHTML, kind: 'text', ratio: contrast(color, background(el))});
  }
  for (const el of document.querySelectorAll(
      'input:not([type=radio]), select, .segment-text-editor, .language-option, .notes-frame,' +
      '.cal-step, .cal-today, .cal-undated, .cal-more')) {
    const style = getComputedStyle(el);
    const color = rgba(style.borderTopColor);
    const ratio = style.borderTopStyle === 'none' || parseFloat(style.borderTopWidth) === 0
      ? 1 : Math.min(contrast(color, background(el)),
        contrast(color, background(el.parentElement)));
    results.push({element: el.outerHTML, kind: 'border', ratio});
  }
  return results;
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 1280])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_workspace_text_and_control_contrast(scheme, width, motion):
    if os.environ.get("ASR_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set ASR_RUN_BROWSER_TESTS=1")
    with sync_playwright() as pw, pw.chromium.launch(headless=True) as browser:
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion=motion)
        css = "\n".join(
            (WEB / f"{name}.css").read_text()
            for name in ("design-system", "shell", "history", "notes", "connections")
        )
        page.set_content(
            f'<html data-theme="{scheme}"><head><style>{css}</style></head>'
            f"<body>{SPECIMENS}</body></html>"
        )
        for hover in (
            None,
            "#hover-recent",
            ".cal-step",
            ".cal-today",
            ".cal-undated",
            ".cal-more",
        ):
            if hover:
                page.locator(hover).hover()
                page.evaluate("document.getAnimations().forEach(animation => animation.finish())")
            results = page.evaluate(MEASURE)
            failures = [r for r in results if r["ratio"] < (4.5 if r["kind"] == "text" else 3)]
            assert not failures, (hover, failures)
