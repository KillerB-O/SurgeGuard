import { ArrowLeft, ArrowUpRight } from "lucide-react";
import { Link } from "react-router-dom";

import { BrandMark, useAmbientCanvas } from "@/components/brand";

type LegalSection = { title: string; paragraphs?: string[]; bullets?: string[] };
type LegalDocument = { title: string; updated: string; intro: string; sections: LegalSection[] };

const privacyPolicy: LegalDocument = {
  title: "SurgeGuard — Privacy Policy",
  updated: "September 2026",
  intro: "SurgeGuard is designed to process information necessary to provide its operational functionality.",
  sections: [
    { title: "1. Information We Collect", paragraphs: ["Depending on the services and integrations used, SurgeGuard may process:"], bullets: ["Account Information — Name, email address, organization information, and authentication information.", "Operational Information — Order identifiers and status, item and work-unit information, fulfillment capacity and warehouse telemetry, dispatch commitments, recovery actions, simulation and configuration data, and related operational records."] },
    { title: "2. Use of Information", paragraphs: ["Information may be used to:"], bullets: ["provide and operate SurgeGuard;", "calculate fulfillment and SLA risk;", "perform simulations;", "generate recovery plans;", "execute authorized actions;", "maintain operational and audit records;", "maintain platform security; and", "improve service reliability and performance."] },
    { title: "3. Data Minimization", paragraphs: ["SurgeGuard follows a data-minimization approach and seeks to process only information reasonably necessary for the relevant functionality or service.", "Where an operational identifier can be used instead of additional personal information, the minimum necessary information should be used."] },
    { title: "4. Data Sharing", paragraphs: ["Information may be processed by service providers and systems necessary to operate SurgeGuard, including infrastructure, authentication, database, integration, commerce, WMS/3PL, and automation providers.", "Customers may also authorize integrations that enable information to be exchanged with their connected systems.", "SurgeGuard does not sell customer operational data."] },
    { title: "5. Third-Party Integrations", paragraphs: ["When customers connect third-party services to SurgeGuard, information may be exchanged with those services according to the configured integration.", "Customers are responsible for ensuring that such integrations are properly authorized and configured."] },
    { title: "6. Data Security", paragraphs: ["SurgeGuard applies reasonable technical and organizational safeguards designed to protect information against unauthorized access, alteration, disclosure, or destruction.", "Security measures may include authentication, access controls, secure communications, database controls, logging, and controlled execution of operational actions."] },
    { title: "7. Data Retention", paragraphs: ["Information is retained only for as long as reasonably necessary to provide the service, maintain operational records, fulfill contractual obligations, resolve disputes, and comply with applicable law.", "Retention periods may vary depending on the nature of the information and the applicable customer agreement."] },
    { title: "8. Privacy Rights", paragraphs: ["Where applicable under law, individuals may have rights concerning their personal data, including access, correction, withdrawal of consent, and deletion.", "Privacy-related requests may be submitted through the contact details provided below."] },
    { title: "9. Children's Data", paragraphs: ["SurgeGuard is intended for business and organizational use and is not directed toward children."] },
    { title: "10. Changes to this Policy", paragraphs: ["We may update this Privacy Policy from time to time to reflect changes in our services, integrations, or applicable legal requirements.", "The updated version will be made available through the platform."] },
    { title: "11. Contact", paragraphs: ["For privacy-related questions or requests:", "Email: privacy@surgeguard.example"] },
  ],
};

const termsAndConditions: LegalDocument = {
  title: "SurgeGuard — Terms & Conditions",
  updated: "September 2026",
  intro: "These Terms govern access to and use of SurgeGuard.",
  sections: [
    { title: "1. Acceptance of Terms", paragraphs: ["By accessing or using SurgeGuard, you agree to these Terms & Conditions. If you access or use SurgeGuard on behalf of an organization, you represent that you are authorized to accept these Terms on its behalf."] },
    { title: "2. Description of Service", paragraphs: ["SurgeGuard is an operational intelligence and fulfillment decision-support platform that enables organizations to:"], bullets: ["monitor fulfillment workload and capacity;", "identify potential dispatch-SLA risks;", "simulate operational scenarios;", "evaluate recovery options; and", "execute approved actions through connected systems.", "SurgeGuard is intended to complement, and not replace, existing warehouse management, order management, inventory, carrier, or e-commerce systems."] },
    { title: "3. Decision Support", paragraphs: ["SurgeGuard provides operational predictions, simulations, recommendations, and insights based on available data and configured assumptions.", "Such outputs are provided for decision-support purposes only and do not constitute a guarantee of fulfillment, dispatch, delivery, or other operational outcomes.", "Users remain responsible for reviewing information and making operational decisions."] },
    { title: "4. Recovery Actions", paragraphs: ["Recovery actions capable of affecting connected systems require appropriate user authorization before execution.", "SurgeGuard does not guarantee successful execution of an approved action where execution depends on third-party systems, integrations, network availability, data accuracy, or other external conditions.", "Where supported, action status will be reflected within SurgeGuard."] },
    { title: "5. Third-Party Integrations", paragraphs: ["SurgeGuard may integrate with third-party commerce platforms, WMS/3PL systems, APIs, databases, webhooks, automation services, and other external systems.", "Third-party services are subject to their own terms, policies, availability, and limitations. SurgeGuard is not responsible for the availability or performance of third-party services.", "Customers are responsible for maintaining appropriate authorization for connected systems."] },
    { title: "6. Customer Responsibilities", paragraphs: ["Customers and authorized users are responsible for:"], bullets: ["providing accurate information and configurations;", "maintaining the confidentiality of account credentials;", "maintaining appropriate access permissions;", "using SurgeGuard only for authorized purposes;", "reviewing operational recommendations before approving actions; and", "promptly reporting suspected unauthorized access or misuse."] },
    { title: "7. Acceptable Use", paragraphs: ["Users must not:"], bullets: ["use SurgeGuard for unlawful purposes;", "access systems or data without authorization;", "interfere with or compromise the platform or connected services;", "introduce malicious or misleading data;", "circumvent security or access controls; or", "use SurgeGuard to perform unauthorized actions."] },
    { title: "8. Service Availability", paragraphs: ["SurgeGuard is provided on an ongoing basis, subject to maintenance, upgrades, technical issues, third-party dependencies, network failures, and other circumstances that may affect availability.", "We do not guarantee uninterrupted or error-free operation."] },
    { title: "9. Intellectual Property", paragraphs: ["All rights, title, and interest in SurgeGuard, including its software, interfaces, documentation, workflows, and underlying technology, remain with SurgeGuard or its applicable licensors.", "Customers retain ownership of data submitted to or processed through SurgeGuard."] },
    { title: "10. Limitation of Liability", paragraphs: ["SurgeGuard provides operational decision-support functionality. Customers remain responsible for their operational decisions and actions.", "To the extent permitted by applicable law, SurgeGuard shall not be liable for losses arising from inaccurate customer-provided information, third-party services or integrations, unauthorized use, or operational decisions made based on information provided by the platform."] },
    { title: "11. Amendments", paragraphs: ["We may update these Terms from time to time to reflect changes to SurgeGuard, our services, or applicable requirements. Updated Terms will be made available through the platform."] },
    { title: "12. Contact", paragraphs: ["For questions regarding these Terms:", "Email: support@surgeguard.example"] },
  ],
};

function LegalFooter() {
  return <footer className="site-footer legal-footer section-shell"><span className="footer-copy">Copyright © 2026 SurgeGuard. All rights reserved.</span><nav className="footer-links" aria-label="Legal"><Link to="/privacy-policy">Privacy Policy</Link><span className="footer-divider">|</span><Link to="/terms-and-conditions">Terms and Conditions</Link></nav></footer>;
}

export function LegalPage({ kind }: { kind: "privacy" | "terms" }) {
  const document = kind === "privacy" ? privacyPolicy : termsAndConditions;
  useAmbientCanvas(`legal-canvas-${kind}`);
  return <div className="site-shell legal-shell" id="top">
    <canvas id={`legal-canvas-${kind}`} className="ambient-canvas" /><div className="grain-layer" />
    <header className="legal-header section-shell"><Link to="/" className="legal-brand"><BrandMark /></Link><Link to="/" className="legal-back"><ArrowLeft size={15} /> Back to SurgeGuard <ArrowUpRight size={13} /></Link></header>
    <main className="legal-main section-shell"><div className="legal-hero"><div className="section-kicker"><i /> SurgeGuard / Legal</div><h1>{document.title}</h1><p className="legal-meta">Last updated: {document.updated}</p><p className="legal-intro">{document.intro}</p></div><article className="legal-document glass-panel">{document.sections.map((section) => <section className="legal-section" key={section.title}><h2>{section.title}</h2>{section.paragraphs?.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}{section.bullets && <ul>{section.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>}</section>)}</article></main>
    <LegalFooter />
  </div>;
}

export function PrivacyPolicyPage() { return <LegalPage kind="privacy" />; }
export function TermsAndConditionsPage() { return <LegalPage kind="terms" />; }
export default LegalPage;
