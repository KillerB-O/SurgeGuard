import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";

import { ProtectedDashboard } from "@/components/ProtectedDashboard";
import { AuthPage } from "@/pages/Auth";
import { LandingPage } from "@/pages/Landing";
import { PrivacyPolicyPage, TermsAndConditionsPage } from "@/pages/Legal";
import { NotFoundPage } from "@/pages/NotFound";
import { ResetPasswordPage } from "@/pages/ResetPassword";
import { SetNewPasswordPage } from "@/pages/SetNewPassword";
import { VerifyEmailPage } from "@/pages/VerifyEmail";

// The dashboard is the heaviest part of the app and needs a session anyway,
// so visitors to the landing page never download it.
const DashboardApp = lazy(() => import("@/dashboard/DashboardApp"));

export default function App() {
  return (
    <Routes>
      {/* Public. */}
      <Route path="/" element={<LandingPage />} />
      <Route path="/privacy-policy" element={<PrivacyPolicyPage />} />
      <Route path="/terms-and-conditions" element={<TermsAndConditionsPage />} />
      <Route path="/auth" element={<AuthPage />} />
      <Route path="/login" element={<AuthPage />} />
      <Route path="/signup" element={<AuthPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      {/* Emailed links land on these two; the backend builds their URLs. */}
      <Route path="/set-new-password" element={<SetNewPasswordPage />} />
      <Route path="/verify-email" element={<VerifyEmailPage />} />

      {/* Needs a session (except in mock mode). */}
      <Route element={<ProtectedDashboard />}>
        <Route
          path="/dashboard/*"
          element={
            <Suspense fallback={null}>
              <DashboardApp />
            </Suspense>
          }
        />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
