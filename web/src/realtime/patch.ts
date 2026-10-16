/**
 * Client side of the structural patch format produced by `src/realtime/patch.py`.
 *
 * Node encoding (see the Python module for the producer):
 *   scalar                                      -> replace the value
 *   ["=", value]                                -> replace with an object, array or null
 *   ["d", {key: node}, [deleted keys]]          -> update an object in place
 *   ["k", field, {keyval: node}, [deleted], order] -> update an array of objects identified by `field`
 */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type PatchNode = Json;

/**
 * Wire key of a keyed-array element. Must match `_k` in patch.py byte for byte:
 * numbers are normalised to a Python float repr ("n:24850.0") and tagged apart from strings.
 */
export function keyOf(key: string | number): string {
  if (typeof key === "number") {
    return "n:" + (Number.isInteger(key) ? key.toFixed(1) : String(key));
  }
  return "s:" + key;
}

type Obj = { [key: string]: Json };

export function applyPatch(base: Json | undefined, node: PatchNode): Json {
  if (!Array.isArray(node)) return node;
  const tag = node[0];
  if (tag === "=") return node[1] as Json;

  if (tag === "d") {
    const result: Obj = { ...((base ?? {}) as Obj) };
    const changes = node[1] as Obj;
    for (const key of Object.keys(changes)) {
      result[key] = applyPatch(result[key], changes[key] as PatchNode);
    }
    for (const key of node[2] as string[]) delete result[key];
    return result;
  }

  if (tag === "k") {
    const field = node[1] as string;
    const changes = node[2] as Obj;
    const deleted = node[3] as (string | number)[];
    const order = node[4] as (string | number)[] | null;
    // A Map keeps insertion order for every key shape (plain objects reorder integer-like keys).
    const byKey = new Map<string, Json>();
    for (const item of (base ?? []) as Obj[]) byKey.set(keyOf(item[field] as string | number), item);
    for (const key of Object.keys(changes)) {
      byKey.set(key, applyPatch(byKey.get(key), changes[key] as PatchNode));
    }
    for (const raw of deleted) byKey.delete(keyOf(raw));
    if (order === null) return [...byKey.values()];
    return order.map((k) => {
      const item = byKey.get(keyOf(k));
      if (item === undefined) throw new PatchError(`order references missing key ${String(k)}`);
      return item;
    });
  }

  throw new PatchError(`unknown patch node ${String(tag)}`);
}

export class PatchError extends Error {}
