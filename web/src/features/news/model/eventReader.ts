export function sourcePlatform(url?: string | null) {
  try {
    const host = new URL(url ?? "").hostname.toLowerCase();
    if (["x.com", "www.x.com", "twitter.com", "www.twitter.com"].includes(host)) return "X";
    if (["t.me", "telegram.me"].includes(host)) return "Telegram";
  } catch {
    /* No platform without a valid recorded URL. */
  }
  return null;
}

import type { NewsEventDetail, NewsUpdateSource, NewsClaim } from "../api/newsQueries";

export function sourceDisplayName(source?: NewsUpdateSource | null, fallback = "来源未记录") {
  const name = source?.origin_id?.trim() || source?.attribution?.trim() || fallback;
  const known: Record<string, string> = {
    "the defiant": "The Defiant",
    "degen news": "degen news",
    "mms news": "mms news",
  };
  return known[name.toLowerCase()] ?? name;
}

export function eventReader(detail: NewsEventDetail) {
  const update = detail.event_update;
  const revision = update?.content_revision;
  const intents = detail.processing?.intents ?? [];
  const sent = intents
    .filter((intent) => intent.state === "sent")
    .sort((a, b) => (b.settled_at_ms ?? 0) - (a.settled_at_ms ?? 0));
  const currentSent = sent.filter(
    (intent) => revision != null && intent.content_revision === revision,
  );
  const notification = detail.processing?.notification;
  const plan =
    notification && notification.content_revision === revision ? notification.plan : null;
  const decisions = new Map((plan?.claim_decisions ?? []).map((row) => [row.claim_ref, row]));
  const lines = new Map(
    [...currentSent]
      .reverse()
      .flatMap((intent) => intent.lines ?? [])
      .map((line) => [line.claim_ref, line.text_zh]),
  );
  const sentRefs = new Set(currentSent.flatMap((intent) => intent.claim_refs ?? []));
  const facts = (update?.claims ?? []).map((claim, index) => ({
    claim,
    number: index + 1,
    decision: decisions.get(claim.ref),
    text: lines.get(claim.ref),
    sent: sentRefs.has(claim.ref),
  }));
  // Preserve adopted numbering while making the delivered information the first reading group.
  facts.sort((a, b) => Number(b.sent) - Number(a.sent) || a.number - b.number);
  return { facts, sent, currentSent, plan, latestSent: currentSent[0], sentRefs };
}

/** Exact citation matches only. Overlapping quotes retain every matching fact number without changing original text. */
export function annotatedSource(text: string, evidenceRef: string, claims: NewsClaim[]) {
  const matches = new Map<string, number[]>();
  claims.forEach((claim, index) =>
    claim.citations.forEach((citation) => {
      if (citation.evidence_ref !== evidenceRef || !citation.quote) return;
      const numbers = matches.get(citation.quote) ?? [];
      if (!numbers.includes(index + 1)) numbers.push(index + 1);
      matches.set(citation.quote, numbers);
    }),
  );
  const spans: { start: number; end: number; numbers: number[] }[] = [];
  for (const [quote, numbers] of matches) {
    let start = text.indexOf(quote);
    while (start !== -1) {
      spans.push({ start, end: start + quote.length, numbers });
      start = text.indexOf(quote, start + quote.length);
    }
  }
  const cuts = [
    ...new Set([0, text.length, ...spans.flatMap((span) => [span.start, span.end])]),
  ].sort((a, b) => a - b);
  const parts: { text: string; numbers: number[] }[] = [];
  for (let index = 0; index < cuts.length - 1; index++) {
    const start = cuts[index],
      end = cuts[index + 1];
    const numbers = [
      ...new Set(
        spans
          .filter((span) => span.start <= start && span.end >= end)
          .flatMap((span) => span.numbers),
      ),
    ].sort((a, b) => a - b);
    const previous = parts.at(-1);
    if (previous && previous.numbers.join(",") === numbers.join(","))
      previous.text += text.slice(start, end);
    else parts.push({ text: text.slice(start, end), numbers });
  }
  return parts;
}

export function eventTiming(detail: NewsEventDetail) {
  const { latestSent, plan } = eventReader(detail);
  const notification = detail.processing?.notification;
  const end =
    latestSent?.settled_at_ms ??
    (plan && notification?.state === "done" ? notification.updated_at_ms : null);
  const received =
    detail.timeline?.find((step) => step.stage === "received")?.at_ms ?? detail.event.opened_at_ms;
  const elapsed = end != null && end >= received ? end - received : null;
  const parts: { label: string; ms: number }[] = [];
  const add = (label: string, ms?: number | null) => {
    if (ms != null && ms >= 0) parts.push({ label, ms });
  };
  const timing = latestSent ? latestSent.plan_timings : plan?.timings;
  if (timing) {
    if (timing.started_at_ms != null && timing.due_at_ms != null)
      add("等待规划", timing.started_at_ms - timing.due_at_ms);
    add("读取与召回", timing.snapshot_ms);
    add("读者判断", timing.judgment_ms);
  }
  const delivery = latestSent?.timings;
  if (delivery) {
    if (delivery.card_finished_at_ms != null && delivery.card_started_at_ms != null)
      add("卡片", delivery.card_finished_at_ms - delivery.card_started_at_ms);
    add("等待发送", delivery.send_slot_wait_ms);
  }
  if (latestSent?.attempted_at_ms != null && latestSent.settled_at_ms != null)
    add("发送", latestSent.settled_at_ms - latestSent.attempted_at_ms);
  return { elapsed, parts, completed: !!latestSent, received, end };
}

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} 秒`;
