import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

import { BrandMark } from "@/components/brand";

export function NotFoundPage() {
  return (
    <div className="not-found">
      <BrandMark />
      <h1>Signal not found.</h1>
      <Link to="/" className="button button-green">
        Return home <ArrowRight size={16} />
      </Link>
    </div>
  );
}
