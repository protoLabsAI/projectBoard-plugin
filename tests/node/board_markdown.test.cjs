// Exercise the actual pinned renderer offline; no CDN or browser is needed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const marked = require('../vendor/marked-12.0.2.cjs');
const source = fs.readFileSync(path.join(__dirname, '../../view/board.js'), 'utf8');
const functions = source.slice(source.indexOf('function renderMarkdown('), source.indexOf('// Upgrade every "saying"'));
const ctx = vm.createContext({});
vm.runInContext(functions, ctx);
const render = (s) => ctx.renderMarkdown(marked, s);

test('fixture matches the production SRI pin', () => {
  const bytes = fs.readFileSync(path.join(__dirname, '../vendor/marked-12.0.2.cjs'));
  const sri = 'sha512-' + crypto.createHash('sha512').update(bytes).digest('base64');
  assert.ok(source.includes('"' + sri + '"'));
});

test('inline, fenced and indented code escape exactly once, including literal entities', () => {
  const code = 'Promise<void> -> None & &lt; &#60; &amp; <img onerror="boom">';
  const escaped = 'Promise&lt;void&gt; -&gt; None &amp; &amp;lt; &amp;#60; &amp;amp; &lt;img onerror=&quot;boom&quot;&gt;';
  assert.equal(render('`' + code + '`'), '<p><code>' + escaped + '</code></p>\n');
  assert.equal(render('```ts\n' + code + '\n```'), '<pre><code class="language-ts">' + escaped + '\n</code></pre>\n');
  assert.equal(render('    ' + code), '<pre><code>' + escaped + '\n</code></pre>\n');
  assert.ok(render('- **type** `Promise<void>`').includes('<strong>type</strong> <code>Promise&lt;void&gt;</code>'));
});

test('headings, emphasis, lists and nested markdown retain formatting', () => {
  const html = render('## Spec\n\n**bold**\n\n1. one\n2. two\n\n- item\n\n> `Promise<void>`');
  for (const tag of ['<h2>Spec</h2>', '<strong>bold</strong>', '<ol>', '<ul>', '<blockquote>', '<code>Promise&lt;void&gt;</code>']) {
    assert.ok(html.includes(tag), tag);
  }
});

test('raw HTML stays literal in prose and nested blocks', () => {
  for (const src of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '**bold** <img src=x onerror=alert(1)>', '> <img src=x onerror=alert(1)>', '- <script>alert(1)</script>']) {
    const html = render(src);
    assert.doesNotMatch(html, /<(?:script|img)\b/i);
    assert.match(html, /&lt;(?:script|img)/);
  }
});

test('link and image sanitization retains dangerous-scheme and new-tab guards', () => {
  for (const scheme of ['javascript', 'data', 'vbscript']) {
    assert.match(render('[bad](' + scheme + ':payload)'), /<a href=/);
    for (const tagName of ['A', 'IMG']) {
      const attrs = {[tagName === 'A' ? 'href' : 'src']: scheme + ':payload'};
      const node = {tagName, getAttribute: (k) => attrs[k], removeAttribute: (k) => delete attrs[k], setAttribute: (k, v) => attrs[k] = v};
      ctx.sanitizeSaying({querySelectorAll: () => [node]});
      assert.equal(attrs.href, undefined);
      assert.equal(attrs.src, undefined);
      if (tagName === 'A') {
        assert.equal(attrs.target, '_blank');
        assert.equal(attrs.rel, 'noopener noreferrer nofollow');
      }
    }
  }
});

test('older callable and parse-only renderer fallbacks keep source HTML escaped', () => {
  const parse = (src, options) => {
    assert.equal(src, '&lt;script&gt;');
    assert.equal(options.breaks, true);
    assert.equal(options.gfm, true);
    return 'fallback';
  };
  assert.equal(ctx.renderMarkdown(parse, '<script>'), 'fallback');
  assert.equal(ctx.renderMarkdown({parse}, '<script>'), 'fallback');
});
