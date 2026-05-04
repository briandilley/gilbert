/**
 * Browser plugin types.
 */

export interface BrowserCredential {
  id: string;
  site: string;
  label: string;
  username: string;
  login_url: string;
}

export interface BrowserCredentialDraft {
  credential_id?: string;
  site: string;
  label: string;
  username: string;
  password?: string;
  login_url: string;
  username_selector?: string;
  password_selector?: string;
  submit_selector?: string;
}

export interface BrowserVncSession {
  id: string;
  vnc_url: string;
  expires_at: string;
}
