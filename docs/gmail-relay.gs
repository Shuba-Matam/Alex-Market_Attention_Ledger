/**
 * Free Gmail relay for Alex Watchlist password-reset codes.
 *
 * Setup (all free):
 * 1. Open https://script.google.com and create a new project.
 * 2. Replace the default code with this file.
 * 3. Change SHARED_SECRET to a long random string.
 * 4. Deploy -> New deployment -> Web app.
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 5. Copy the /exec URL and set it as GMAIL_RELAY_URL on the backend.
 * 6. Set the same secret as GMAIL_RELAY_SECRET.
 *
 * MailApp sends from the Google account that owns this script. Consumer Gmail
 * accounts have a daily quota of about 100 recipients, which is plenty here.
 */
const SHARED_SECRET = 'replace-with-a-long-random-string';

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SHARED_SECRET) {
      return ContentService.createTextOutput('forbidden');
    }
    if (!body.to || !body.subject || !body.text) {
      return ContentService.createTextOutput('bad request');
    }
    MailApp.sendEmail({
      to: body.to,
      subject: body.subject,
      body: body.text,
    });
    return ContentService.createTextOutput('ok');
  } catch (error) {
    return ContentService.createTextOutput('error');
  }
}
