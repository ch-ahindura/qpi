interface Props {
  message: string;
}

/**
 * Why a job failed, shown so it can be acted on.
 *
 * A failed job used to render nothing at all — the visualiser reported "No
 * counts data available", which is true and useless, and the reason the driver
 * had already sent sat unread in the record. Compiler errors are the common
 * case and they are long: quantify and qblox both attach a paragraph of
 * guidance to a one-line problem. So the first line leads, and the rest is kept
 * verbatim underneath rather than truncated, because that paragraph is usually
 * the fix.
 */
export function JobError({ message }: Props) {
  const lines = message.trim().split("\n");
  const headline = lines[0];
  const detail = lines.slice(1).join("\n").trim();

  return (
    <div className="w-full h-full flex flex-col gap-3 items-start justify-start overflow-auto text-left">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase font-semibold tracking-wider px-2 py-0.5 rounded-full border border-red-500/30 bg-red-500/10 text-red-400">
          Job failed
        </span>
      </div>
      <p className="font-geist text-sm text-gray-900 dark:text-white break-words">
        {headline}
      </p>
      {detail && (
        <pre className="w-full font-mono text-xs text-gray-500 dark:text-zinc-400 whitespace-pre-wrap break-words">
          {detail}
        </pre>
      )}
    </div>
  );
}
