"""DOM-pruning fallback — run in-page when the Accessibility Tree is thin.

Deliberately small — three pruning rules only:
  1. Drop invisible elements (display:none / visibility:hidden / opacity:0).
  2. Drop structural chrome that rarely carries task-relevant text
     (aside, nav, footer, script, style, noscript, iframe).
  3. Collapse lists with >5 similar items into "first 3 + [... +N items]".

Returns a plain-text rendering with inline links and form affordances.
"""

from __future__ import annotations

_DOM_PRUNE_JS = r"""
(function(){
  const DROP = new Set(['SCRIPT','STYLE','NOSCRIPT','ASIDE','NAV','FOOTER','IFRAME','SVG','CANVAS']);
  const HEAD = new Set(['H1','H2','H3','H4']);
  function visible(el){
    try {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
      if (parseFloat(s.opacity) === 0) return false;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return false;
      return true;
    } catch (e) { return true; }
  }
  function walk(node, depth){
    if (!node || depth > 14) return '';
    if (node.nodeType === 3) {
      const t = (node.textContent || '').trim();
      return t ? t + ' ' : '';
    }
    if (node.nodeType !== 1) return '';
    const el = node;
    const tag = el.tagName;
    if (DROP.has(tag)) return '';
    if (!visible(el)) return '';
    const kids = [];
    for (const c of el.childNodes) {
      const s = walk(c, depth + 1);
      if (s) kids.push(s);
    }
    const t = tag.toLowerCase();
    if (t === 'ul' || t === 'ol') {
      if (kids.length > 5) {
        return kids.slice(0,3).join(' ') + ' [... +' + (kids.length - 3) + ' items] ';
      }
      return kids.join(' ');
    }
    const inner = kids.join(' ').replace(/\s+/g,' ').trim();
    if (t === 'a' && el.href && inner) return '[' + inner + '](' + el.href + ') ';
    if (t === 'input') {
      const ty = el.type || 'text';
      const ph = el.placeholder || '';
      const val = el.value || '';
      return '<' + ty + (ph ? ' ph="' + ph + '"' : '') + (val ? ' val="' + val + '"' : '') + '> ';
    }
    if (t === 'button') {
      const label = inner || el.getAttribute('aria-label') || '';
      return label ? '[btn:' + label + '] ' : '';
    }
    if (HEAD.has(tag) && inner) return '\n## ' + inner + '\n';
    return inner + ' ';
  }
  const out = walk(document.body, 0).replace(/\s{3,}/g,'\n').trim();
  return out.slice(0, 30000);
})()
"""


def dom_prune_script() -> str:
    """Return the JS expression the CDP Runtime.evaluate should run."""
    return _DOM_PRUNE_JS
