// Layout check used by tests/test_layout.py: every visible element in <main> stays inside its
// nearest container (argument: a CSS selector list) and is never wider than the screen.
(containers) => {
  const vw = document.documentElement.clientWidth;
  const problems = [];
  const label = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '') +
    ' "' + (el.innerText || el.value || '').trim().slice(0, 40).replace(/\s+/g, ' ') + '"';
  for (const el of document.querySelectorAll('main *')) {
    if (el.closest('.trap, [hidden], template, script, style, svg *')) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.position === 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;  // visually hidden inputs and empty spans
    if (r.width > vw + 0.5) problems.push(`${label(el)} is ${Math.round(r.width)}px wide, screen ${vw}px`);
    const box = el.parentElement && el.parentElement.closest(containers);
    if (!box) continue;
    const b = box.getBoundingClientRect();
    const over = r.right - b.right;
    if (over > 0.5) problems.push(`${label(el)} sticks out ${Math.round(over)}px past ${label(box)}`);
  }
  return problems;
}
