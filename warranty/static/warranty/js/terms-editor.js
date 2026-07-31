// Wires a contenteditable Warranty Terms box up to its Bold/Heading/Paragraph/
// Bullet-list toolbar. Shared by the warranty letter form and the Brand Terms
// add/edit form, since both use the same rich-text editing pattern.
function initTermsEditor(editorEl, toolbarEl) {
    toolbarEl.querySelectorAll('button[data-cmd]').forEach(btn => {
        // Keep the text selection alive -- without this, clicking the button
        // steals focus from the editor first and execCommand loses the
        // selection to apply the format to.
        btn.addEventListener('mousedown', e => e.preventDefault());
        btn.addEventListener('click', () => {
            editorEl.focus();
            switch (btn.dataset.cmd) {
                case 'bold': document.execCommand('bold'); break;
                case 'heading': document.execCommand('formatBlock', false, 'h3'); break;
                case 'paragraph': document.execCommand('formatBlock', false, 'p'); break;
                case 'list': document.execCommand('insertUnorderedList'); break;
            }
        });
    });
}
