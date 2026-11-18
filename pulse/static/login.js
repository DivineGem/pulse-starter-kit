// Use an explicit same-origin request marker and a JSON body; credentials are
// exchanged for an HttpOnly session cookie rather than stored in browser JS.
const form = document.querySelector('#login-form');
const errorBox = document.querySelector('#login-error');

form.addEventListener('submit', async event => {
  event.preventDefault();
  errorBox.hidden = true;
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Pulse-Request': '1' },
      body: JSON.stringify({
        username: form.elements.username.value,
        password: form.elements.password.value
      })
    });
    if (!response.ok) throw new Error('Username or password was not accepted.');
    window.location.assign('/');
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
});
