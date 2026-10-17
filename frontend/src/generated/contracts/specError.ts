/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`. */

export type Path = string;
export type Code = string;
export type Message = string;

/**
 * One structured validation problem; ``path`` is a dotted spec location.
 */
export interface SpecError {
  path: Path;
  code: Code;
  message: Message;
}
