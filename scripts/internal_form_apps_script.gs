/**
 * Google Apps Script for the internal @google.com-restricted Google Form.
 *
 * Form setup (inside Google Workspace, restricted to @google.com users):
 *   1. Settings -> Responses -> Enable "Restrict to users in Google and its trusted organizations"
 *      and "Collect email addresses" (Verified).
 *   2. Question 1 (Short answer, required): "Discord User ID (auto-filled — do not edit)"
 *      Get its pre-fill link via ⋮ -> Get pre-filled link, put `{user_id}` in Q1, and copy that URL
 *      into Cloud Run env var `INTERNAL_FORM_URL_TEMPLATE`.
 *   3. Question 2 (optional, Short/Paragraph): "Additional Notes (optional)"
 *   4. In Apps Script -> Project Settings -> Script Properties, add:
 *      - VERIFY_ENDPOINT_URL: https://<your-cloud-run-url>/internal-verify
 *      - INTERNAL_FORM_SECRET: <same secret stored in GCP Secret Manager>
 *   5. In Apps Script -> Triggers, add a trigger: onFormSubmit -> From form -> On form submit.
 */
function onFormSubmit(e) {
  var props = PropertiesService.getScriptProperties();
  var endpointUrl = props.getProperty("VERIFY_ENDPOINT_URL");
  var secret = props.getProperty("INTERNAL_FORM_SECRET");

  if (!endpointUrl || !secret) {
    throw new Error("Missing VERIFY_ENDPOINT_URL or INTERNAL_FORM_SECRET in Script Properties.");
  }

  var email = (e.response.getRespondentEmail() || "").trim().toLowerCase();
  if (!email.endsWith("@google.com")) {
    Logger.log("Rejected non-@google.com submission: " + email);
    return;
  }

  var itemResponses = e.response.getItemResponses();
  var userId = (itemResponses[0] ? String(itemResponses[0].getResponse()) : "").trim();
  var notes = itemResponses[1] ? String(itemResponses[1].getResponse()).trim() : "";

  if (!/^\d{17,20}$/.test(userId)) {
    throw new Error("Invalid Discord User ID submitted: " + userId);
  }

  var resp = UrlFetchApp.fetch(endpointUrl, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + secret,
    },
    payload: JSON.stringify({
      user_id: userId,
      notes: notes,
    }),
    muteHttpExceptions: true,
  });

  var code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error("Verification API returned HTTP " + code + ": " + resp.getContentText());
  }
  Logger.log("Verified Discord user " + userId + ": " + resp.getContentText());
}
