import type {
  Attachment,
  AttachmentAdapter,
  CompleteAttachment,
  PendingAttachment,
} from "@assistant-ui/react";

/** Read a File as a data: URL (works in browser + Node fallbacks). */
async function getFileDataURL(file: File): Promise<string> {
  if (typeof FileReader === "undefined") {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]!);
    const b64 = btoa(binary);
    const mime = file.type || "application/octet-stream";
    return `data:${mime};base64,${b64}`;
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = (error) => reject(error);
    reader.readAsDataURL(file);
  });
}

function guessType(file: File): "document" | "file" {
  const mime = (file.type || "").toLowerCase();
  const name = file.name.toLowerCase();
  if (mime.startsWith("text/") || mime === "application/pdf" || name.endsWith(".pdf")) {
    return "document";
  }
  return "file";
}

/**
 * Catch-all adapter for non-image uploads. Emits a `file` content part with a
 * data URL so the send path can call Hermes `pdf.attach` / `file.attach`.
 * Must be last in CompositeAttachmentAdapter (accept = "*").
 */
export class HermesFileAttachmentAdapter implements AttachmentAdapter {
  public accept = "*";

  public async add(state: { file: File }): Promise<PendingAttachment> {
    return {
      id: state.file.name + "-" + crypto.randomUUID(),
      type: guessType(state.file),
      name: state.file.name,
      contentType: state.file.type || "application/octet-stream",
      file: state.file,
      status: { type: "requires-action", reason: "composer-send" },
    };
  }

  public async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
    const data = await getFileDataURL(attachment.file);
    return {
      ...attachment,
      status: { type: "complete" },
      content: [
        {
          type: "file",
          data,
          mimeType: attachment.contentType || attachment.file.type || "application/octet-stream",
          filename: attachment.name,
        },
      ],
    };
  }

  public async remove(_attachment: Attachment): Promise<void> {
    // noop — bytes live only in the composer until send
  }
}

export function isPdfAttachment(name: string, mimeType?: string): boolean {
  const mime = (mimeType || "").toLowerCase();
  const lower = name.toLowerCase();
  return mime === "application/pdf" || lower.endsWith(".pdf");
}
