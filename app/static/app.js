// Preserve the clicked decision's name/value while preventing accidental double submission.
document.addEventListener('submit', event => {
  const form = event.target;
  if (form.method.toLowerCase() !== 'post' || !form.checkValidity()) return;
  if (form.dataset.submitting) { event.preventDefault(); return; }
  form.dataset.submitting = 'true';
  const button = event.submitter;
  if (button && button.name) {
    const value = document.createElement('input');
    value.type = 'hidden'; value.name = button.name; value.value = button.value;
    form.appendChild(value);
  }
  form.querySelectorAll('button').forEach(b => b.disabled = true);
  if (button) { button.textContent = 'Working…'; button.setAttribute('aria-busy', 'true'); }
});
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });

// Refresh guided runs only while a durable item awaits an n8n worker.
const runPage = document.querySelector('[data-auto-refresh="true"]');
if (runPage) {
  window.setInterval(() => {
    const active = document.activeElement;
    const editing = active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
    const submitting = document.querySelector('form[data-submitting="true"]');
    if (!editing && !submitting && document.visibilityState === 'visible') location.reload();
  }, 5000);
}
