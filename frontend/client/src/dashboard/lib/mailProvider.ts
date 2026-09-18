/**
 * Maps an email address to a direct link to that provider's inbox.
 *
 * The gap this closes is small and real: someone reads "check your email",
 * then has to go find the right tab, or remember which provider the address
 * even belongs to. A button that opens the inbox — ideally already filtered
 * to our message — removes the step where people give up.
 *
 * Only webmail providers can be linked. A company address on its own domain
 * might be Outlook, Gmail, or a desktop client, and guessing wrong is worse
 * than not offering: a dead-end link erodes trust in the whole flow. Those
 * get no button, which is the honest outcome.
 */

export interface MailProvider {
  /** Shown on the button, e.g. "Gmail" -> "Open Gmail". */
  name: string;
  url: string;
}

/**
 * Deep links, most specific first. Where a provider supports a search
 * parameter we use it, so the message is already on screen rather than
 * somewhere in an inbox.
 */
const PROVIDERS: Array<{ domains: string[]; name: string; url: string }> = [
  {
    domains: ["gmail.com", "googlemail.com"],
    name: "Gmail",
    url: "https://mail.google.com/mail/u/0/#search/SurgeGuard",
  },
  {
    domains: ["outlook.com", "hotmail.com", "live.com", "msn.com"],
    name: "Outlook",
    url: "https://outlook.live.com/mail/0/",
  },
  {
    domains: ["yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com"],
    name: "Yahoo Mail",
    url: "https://mail.yahoo.com/",
  },
  {
    domains: ["icloud.com", "me.com", "mac.com"],
    name: "iCloud Mail",
    url: "https://www.icloud.com/mail",
  },
  {
    domains: ["proton.me", "protonmail.com", "pm.me"],
    name: "Proton Mail",
    url: "https://mail.proton.me/u/0/inbox",
  },
  {
    domains: ["zoho.com", "zohomail.com"],
    name: "Zoho Mail",
    url: "https://mail.zoho.com/zm/",
  },
];

/** Returns the provider for an address, or null when it cannot be known. */
export function mailProviderFor(email: string): MailProvider | null {
  const at = email.lastIndexOf("@");
  if (at === -1) return null;

  const domain = email.slice(at + 1).trim().toLowerCase();
  if (!domain) return null;

  const match = PROVIDERS.find((provider) => provider.domains.includes(domain));
  return match ? { name: match.name, url: match.url } : null;
}
