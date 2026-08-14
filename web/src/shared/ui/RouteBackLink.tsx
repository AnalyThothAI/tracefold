import { ArrowLeft } from "lucide-react";
import { Link, useInRouterContext } from "react-router-dom";

import styles from "./RouteBackLink.module.css";

export function RouteBackLink({
  ariaLabel,
  label,
  state,
  to,
}: {
  ariaLabel: string;
  label: string;
  state?: unknown;
  to: string;
}) {
  const inRouter = useInRouterContext();
  const content = (
    <>
      <ArrowLeft aria-hidden />
      <span>{label}</span>
    </>
  );

  if (inRouter) {
    return (
      <Link aria-label={ariaLabel} className={styles.link} state={state} to={to}>
        {content}
      </Link>
    );
  }

  return (
    <a aria-label={ariaLabel} className={styles.link} href={to}>
      {content}
    </a>
  );
}
