/** Talking to the backend, and the shapes it answers with. */

import { call as backend } from "@decky/api";

export interface Game {
  appid: number;
  name: string;
  cloud: boolean | null;
  saves: boolean | null;
  on: boolean;
  set: string | null;
  /** "library: <name>" or "game: <one game>", for a game tied to a tree. */
  set_caption?: string;
  shortcut: boolean;
  hub: string;
  hub_device: string;
}

export interface Step {
  label: string;
  ok: boolean;
  detail: string;
}

export interface LogLine {
  text: string;
  kind: "bad" | "warn" | "good" | "";
}

/**
 * One tree of savepick.json: an emulator library (many games, each its own
 * save) or, with `one_game`, one emulator game. `label` names the emulator
 * that owns the save, never the frontend.
 */
export interface SaveSet {
  name: string;
  root: string;
  devices: number;
  kind?: "library" | "game";
  one_game?: string;
  label?: string;
}

/** One save of a game two devices both played. `id` goes to storeChoose. */
export interface GameChoice {
  id: string;
  device: string;
  when: string;
}

/** One game of an emulator library, as library_games hands it out. */
export interface LibraryGame {
  name: string;
  title: string;
  system: string;
  label: string;
  when: string;
  device: string;
  line: string;
  two: boolean;
  heads: number;
  /** Null when the store did not say which saves they are. */
  choices: GameChoice[] | null;
}

export interface LibraryGames {
  library: string;
  games: LibraryGame[];
  total: number;
  all: number;
  error: string | null;
}

export interface SyncSettings {
  url: string;
  apikey: string;
  folder: string;
  hub_id: string;
  hub_name: string;
  device_dir: string;
}

export interface EngineState {
  engine: string;
  installed: boolean;
  current: boolean;
  have_source: boolean;
  ludusavi: string;
  ludusavi_found: boolean;
  ludusavi_archive: boolean;
  device_name: string;
  settings_file: string;
  log: string;
  steam: string;
}

/**
 * Call a backend method. A failed call is null, never a thrown error.
 *
 * Arguments are positional, in the order main.py declares them. That is how
 * the loader hands them over, so a name here would mean nothing to it.
 */
export async function call<T = any>(method: string, ...args: any[]): Promise<T | null> {
  try {
    return await backend<any[], T>(method, ...args);
  } catch (error) {
    console.error("Blockslot: " + method + " failed", error);
    return null;
  }
}

/** Steam's own API, read loosely: the types ship a partial view of it. */
const steam = (): any => (window as any).SteamClient;

/**
 * What Steam currently has as this game's launch option.
 *
 * The file on disk is stale while Steam runs, so it is never the source here.
 * RegisterForAppDetails fires once with the current details, which is all this
 * needs, and the registration is dropped straight after.
 */
export async function currentLaunchOptions(appid: number): Promise<string> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value: string) => {
      if (!done) {
        done = true;
        resolve(value);
      }
    };
    try {
      const handle = steam().Apps.RegisterForAppDetails(appid, (details: any) => {
        finish(details?.strLaunchOptions ?? "");
        try {
          handle.unregister();
        } catch (error) {
          try {
            (handle as any)();
          } catch (ignored) {
            /* some builds hand back a plain function */
          }
        }
      });
    } catch (error) {
      finish("");
    }
    // Never leave a toggle waiting on a callback that does not come.
    setTimeout(() => finish(""), 1500);
  });
}

/**
 * Turn sync on or off for one game.
 *
 * Steam is asked to set the option rather than the file being edited, because
 * Steam holds that file in memory and would write over any edit on exit.
 * Returns an error string, the word "choose" plus the sets to pick from, or
 * null when it worked.
 */
export async function setSync(
  game: Game,
  on: boolean,
  saveSet?: string
): Promise<{ error?: string; chooseSet?: string[] }> {
  const existing = await currentLaunchOptions(game.appid);
  const answer = await call<any>(
    "launch_option",
    game.appid,
    on,
    existing,
    saveSet ?? null
  );
  if (!answer?.ok) {
    if (answer?.choose_set?.length) return { chooseSet: answer.choose_set };
    return { error: answer?.error || "Could not work out what to set." };
  }
  try {
    steam().Apps.SetAppLaunchOptions(game.appid, answer.option);
  } catch (error) {
    return { error: "Steam refused the change: " + error };
  }
  return {};
}

// ------------------------------------------------------------------ store

/** One snapshot waiting in this device's queue. */
export interface QueuedSave {
  id: string;
  game: string;
  bytes: number;
  progress: { done: number; total: number } | null;
}

/**
 * What the store daemon says. `configured` false means savepick.json has no
 * store section, and the panel then shows nothing about a store at all.
 */
export interface StoreStatus {
  configured: boolean;
  running?: boolean;
  store?: string;
  device?: string;
  queue?: QueuedSave[];
  error?: { kind: string; message: string } | null;
  last_ok?: string | null;
  line?: string;
  tone?: "good" | "warn" | "bad";
}

/** One head of a forked game: a real save from one device. */
export interface ForkHead {
  id: string;
  device: string;
  created: string | null;
  played_end: string | null;
}

export interface Fork {
  game: string;
  label: string;
  heads: ForkHead[];
}

export const storeStatus = () => call<StoreStatus>("store_status");

/** Set this device up from a BlockSlot server's pairing code. */
export const storePair = (address: string, code: string) =>
  call<{ ok: boolean; error?: string; status?: StoreStatus; address?: string }>(
    "store_pair", address, code);

export const storeUploadNow = () =>
  call<{ ok: boolean; error?: string }>("store_upload_now");

export const storeForks = () =>
  call<{ forks: Fork[]; error: string | null }>("store_forks");

export const storeChoose = (game: string, snapId: string) =>
  call<{ ok: boolean; error?: string; merge?: string }>("store_choose", game, snapId);

/** At most `limit` games of one library, filtered by title. */
export const libraryGames = (
  library: string,
  search: string,
  limit = 200,
  refresh = false
) => call<LibraryGames>("library_games", library, search, limit, refresh);
