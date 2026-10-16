/* Local markdown-it 15.0.3 browser build; upstream license: markdown-it.LICENSE.txt.
   Source: https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.3.tgz
   Render only AI replies/plans, never user messages, tool output or site HTML. */
(() => {
  'use strict';

  const parser = typeof window.markdownit === 'function'
    ? window.markdownit({html: false, breaks: true, linkify: false, typographer: false, maxNesting: 32}).disable('image')
    : null;
  if (parser) {
    parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet('target', '_blank');
      tokens[index].attrSet('rel', 'noopener noreferrer');
      return renderer.renderToken(tokens, index, options);
    };
  }

  function render(text) {
    const source = String(text ?? '');
    if (parser) return parser.render(source);
    // A missing asset must not discard text or collapse line breaks.
    return source.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\r?\n/g, '<br>');
  }

  function source(node) {
    return node?.dataset.aiMarkdownSource ?? node?.innerText ?? node?.textContent ?? '';
  }

  function renderInto(node, text) {
    if (!node) return null;
    const value = String(text ?? '');
    if (node.tagName === 'P') {
      // Markdown contains block elements; nesting them in <p> corrupts history
      // when it is serialized, reloaded or archived by the browser.
      const replacement = node.ownerDocument.createElement('div');
      replacement.id = node.id;
      replacement.className = node.className;
      node.replaceWith(replacement);
      node = replacement;
    }
    node.classList.add('ai-markdown');
    if (node.parentElement?.classList.contains('ai-run-final')) node.classList.add('ai-run-summary');
    node.dataset.aiMarkdownSource = value;
    node.dataset.aiMarkdownRendered = '1';
    node.innerHTML = render(value);
    return node;
  }

  function renderAll(root) {
    for (const node of root.querySelectorAll('.ai-run-final > p, .ai-run-final > .ai-run-summary, .ai-plan-box > p')) {
      if (node.dataset.aiMarkdownRendered !== '1' && !node.classList.contains('is-typing')) renderInto(node, source(node));
    }
  }

  window.AIchemyHatcheryMarkdown = Object.freeze({render, source, renderInto, renderAll});
})();
