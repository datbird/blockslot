/**
 * The full Blockslot page, reached from the Quick Access panel.
 *
 * Same five sections as the desktop window, in the shape Steam gives a page:
 * a rail on the left, one section at a time on the right. The Quick Access
 * panel keeps only the thing you want mid-session, which is flipping sync on
 * for the game you are about to play.
 */

import {
  ConfirmModal,
  DialogButton,
  Dropdown,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
  showModal,
  SidebarNavigation,
  ToggleField,
} from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

import {
  call,
  EngineState,
  Fork,
  ForkHead,
  Game,
  GameChoice,
  LibraryGame,
  LibraryGames,
  libraryGames,
  LogLine,
  SaveSet,
  setSync,
  Step,
  storeChoose,
  storeForks,
  storePair,
  StoreStatus,
  storeStatus,
  storeUploadNow,
  SyncSettings,
} from "./api";
import { SafeButton, Text } from "./controls";

const GOOD = "#57d364";
const WARN = "#e3b341";
const BAD = "#f2685c";

/** A line of state: a coloured dot, what it is, and what it says. */
const State: FC<{ ok: boolean; label: string; detail?: string }> = ({
  ok,
  label,
  detail,
}) => (
  <Field
    label={
      <span>
        <span style={{ color: ok ? GOOD : WARN, marginRight: "8px" }}>
          {ok ? "●" : "○"}
        </span>
        {label}
      </span>
    }
    description={detail}
    bottomSeparator="standard"
  />
);

// ------------------------------------------------------------------ store

const TONE: Record<string, string> = { good: GOOD, warn: WARN, bad: BAD };

/** Bytes as a person reads them, to one decimal place. */
function size(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " bytes";
}

/** A store time in this device's own clock, short. */
function when(iso: string | null | undefined): string {
  if (!iso) return "an unknown time";
  const date = new Date(iso);
  if (isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/**
 * Where saves go, and whether they got there. Only drawn when a store is set
 * up; until then the panel is exactly the Syncthing one it always was.
 *
 * Asked again every few seconds while the panel is open, because an upload
 * that is running is the one thing here that changes on its own.
 */
export const StoreSection: FC = () => {
  const [status, setStatus] = useState<StoreStatus | null>(null);
  const [forks, setForks] = useState<Fork[]>([]);
  const [note, setNote] = useState("");

  const loadStatus = async () => {
    const answer = await storeStatus();
    if (answer) setStatus(answer);
  };

  const loadForks = async () => {
    const answer = await storeForks();
    if (answer) setForks(answer.forks || []);
  };

  useEffect(() => {
    loadStatus().then(loadForks);
    const timer = setInterval(loadStatus, 4000);
    return () => clearInterval(timer);
  }, []);

  if (!status?.configured) return null;

  const queue = status.queue || [];

  const pick = (fork: Fork, head: ForkHead) =>
    showModal(
      <ConfirmModal
        strTitle={"Use the " + head.device + " save of " + fork.game + "?"}
        strDescription={
          "Every device uses this save from its next launch. The other save " +
          "stays on the store as history, so nothing is deleted."
        }
        strOKButtonText={"Use the " + head.device + " save"}
        onOK={async () => {
          setNote("Recording your choice ...");
          const answer = await storeChoose(fork.game, head.id);
          setNote(
            answer?.ok
              ? fork.game + " now uses the " + head.device + " save."
              : answer?.error || "Could not record the choice."
          );
          await loadStatus();
          await loadForks();
        }}
      />
    );

  return (
    <PanelSection title="Store">
      <PanelSectionRow>
        <Field
          label={
            <span>
              <span style={{ color: TONE[status.tone || "warn"], marginRight: "8px" }}>
                {status.tone === "good" ? "●" : "○"}
              </span>
              {status.line}
            </span>
          }
          description={status.last_ok ? "Last upload " + when(status.last_ok) : undefined}
          bottomSeparator="standard"
        />
      </PanelSectionRow>
      {queue.map((item) =>
        item.progress && item.progress.total > 0 ? (
          <PanelSectionRow key={item.id}>
            <ProgressBarWithInfo
              label={item.game}
              nProgress={Math.round((100 * item.progress.done) / item.progress.total)}
              sOperationText={
                size(item.progress.done) + " of " + size(item.progress.total)
              }
            />
          </PanelSectionRow>
        ) : (
          <PanelSectionRow key={item.id}>
            <Field label={item.game} description={"Waiting, " + size(item.bytes)} />
          </PanelSectionRow>
        )
      )}
      <PanelSectionRow>
        <Focusable style={{ display: "flex", gap: "8px" }}>
          <SafeButton
            disabled={queue.length === 0 || status.running === false}
            onClick={async () => {
              const answer = await storeUploadNow();
              setNote(answer?.ok ? "Uploading ..." : answer?.error || "Could not start.");
              await loadStatus();
            }}
          >
            Upload now
          </SafeButton>
        </Focusable>
      </PanelSectionRow>
      {forks.map((fork) => (
        <PanelSectionRow key={fork.game}>
          <Field
            label={fork.label}
            description="Both are real play. Pick the one to keep playing on."
            bottomSeparator="none"
          />
          <Focusable style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
            {fork.heads.map((head) => (
              <SafeButton key={head.id} onClick={() => pick(fork, head)}>
                {"Use the " + head.device + " save, " + when(head.played_end || head.created)}
              </SafeButton>
            ))}
          </Focusable>
        </PanelSectionRow>
      ))}
      {note ? (
        <PanelSectionRow>
          <Field label={note} bottomSeparator="none" />
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
};

// ------------------------------------------------------------------ games

/** Show the library at once, then again with what the hub holds. */
export async function loadWithHub(
  load: () => Promise<void>,
  setBusy: (text: string) => void
) {
  setBusy("Reading your library ...");
  await load();
  setBusy("Checking where your saves are ...");
  await call("read_hub");
  await load();
  setBusy("");
}

/** Turning sync on or off for one row, the same from the panel and the page. */
export function useSyncToggle(
  setGames: (update: (rows: Game[]) => Game[]) => void,
  setNote: (text: string) => void
) {
  const flip = async (game: Game, on: boolean, saveSet?: string) => {
    setNote("");
    const answer = await setSync(game, on, saveSet);
    if (answer.chooseSet) {
      showModal(
        <ChooseSet
          game={game}
          sets={answer.chooseSet}
          onPick={(name) => flip(game, on, name)}
        />
      );
      return;
    }
    if (answer.error) {
      setNote(answer.error);
      return;
    }
    setGames((rows) =>
      rows.map((row) => (row.appid === game.appid ? { ...row, on } : row))
    );
    setNote(
      on
        ? game.name + " will sync from its next launch."
        : game.name + " is no longer synced."
    );
  };
  return flip;
}

/** One toggle per game. Turning one off asks first, turning one on does not. */
export const GameRows: FC<{
  games: Game[];
  busy: string;
  flip: (game: Game, on: boolean) => void;
}> = ({ games, busy, flip }) => (
  <>
    {games.map((game) => (
      <PanelSectionRow key={game.appid}>
        <ToggleField
          label={game.name}
          description={describe(game)}
          checked={game.on}
          onChange={(value: boolean) => {
            if (!value && game.on) {
              showModal(
                <ConfirmModal
                  strTitle={"Stop syncing " + game.name + "?"}
                  strDescription={
                    "Its saves stay where they are, here and on the hub. " +
                    "They just stop being carried between your machines."
                  }
                  strOKButtonText="Stop syncing"
                  onOK={() => flip(game, false)}
                />
              );
              return;
            }
            flip(game, value);
          }}
        />
      </PanelSectionRow>
    ))}
    {games.length === 0 && !busy ? (
      <PanelSectionRow>
        <Field
          label="Nothing to show"
          description="Every installed game is covered by Steam Cloud."
        />
      </PanelSectionRow>
    ) : null}
  </>
);

const GamesPage: FC = () => {
  const [games, setGames] = useState<Game[]>([]);
  const [hideCloud, setHideCloud] = useState(true);
  const [busy, setBusy] = useState("Reading your library ...");
  const [note, setNote] = useState("");
  const flip = useSyncToggle(setGames, setNote);

  const load = async (includeCloud: boolean) => {
    const answer = await call<any>("games", includeCloud);
    if (answer?.games) setGames(answer.games);
  };

  useEffect(() => {
    loadWithHub(() => load(!hideCloud), setBusy);
  }, []);

  return (
    <>
      <PanelSection title="Games">
        <PanelSectionRow>
          <ToggleField
            label="Hide games Steam Cloud covers"
            description="Steam already carries those. This tool is for the rest."
            checked={hideCloud}
            onChange={async (value: boolean) => {
              setHideCloud(value);
              setBusy("Reading ...");
              await load(!value);
              setBusy("");
            }}
          />
        </PanelSectionRow>
        {busy ? (
          <PanelSectionRow>
            <Field label={busy} />
          </PanelSectionRow>
        ) : null}
        {note ? (
          <PanelSectionRow>
            <Field label={note} bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
      </PanelSection>
      <PanelSection>
        <GameRows games={games} busy={busy} flip={flip} />
      </PanelSection>
    </>
  );
};

/** The line under a game: where its save last landed, or why it cannot sync. */
function describe(game: Game): string {
  const parts: string[] = [];
  if (game.hub) {
    parts.push(
      "saved " + game.hub + (game.hub_device ? " from " + game.hub_device : "")
    );
  } else {
    parts.push("not saved yet");
  }
  if (game.set) parts.push(game.set_caption || "library: " + game.set);
  if (game.cloud) parts.push("Steam Cloud covers this");
  if (game.shortcut && !game.set) parts.push("it will ask which emulator library or game it uses");
  return parts.join("   ");
}

/** Ask which emulator library or game a non-Steam game uses. Its app id tells savepick nothing. */
const ChooseSet: FC<{
  game: Game;
  sets: string[];
  onPick: (name: string) => void;
  closeModal?: () => void;
}> = ({ game, sets, onPick, closeModal }) => (
  <ConfirmModal
    strTitle={"Which saves does " + game.name + " use?"}
    strDescription={
      game.name + " is not a Steam game, so its app id says nothing about saves."
    }
    bAlertDialog={true}
    strOKButtonText="Cancel"
    onOK={() => closeModal?.()}
  >
    <Focusable style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
      {sets.map((name) => (
        <DialogButton
          key={name}
          onClick={() => {
            closeModal?.();
            onPick(name);
          }}
        >
          {name}
        </DialogButton>
      ))}
    </Focusable>
  </ConfirmModal>
);

// ------------------------------------------------------------------- sync

/** Pairing with a BlockSlot server: its address and a short code from its
 * Devices page. A code can be typed on the Deck's keyboard; a full setup
 * code cannot. */
const PairSection: FC<{ title: string; intro: string; onPaired: (store: string) => void }> = ({
  title,
  intro,
  onPaired,
}) => {
  const [address, setAddress] = useState("");
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const pair = async () => {
    setBusy(true);
    setNote("Pairing ...");
    const answer = await storePair(address, code);
    setBusy(false);
    if (!answer?.ok) {
      setNote(answer?.error || "Could not pair.");
      return;
    }
    setCode("");
    setNote("Paired. Saves now go to " + (answer.status?.store || "the store") + ".");
    onPaired(answer.status?.store || "the store");
  };

  return (
    <PanelSection title={title}>
      <PanelSectionRow>
        <div>{intro}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <Text
          label="Server address"
          value={address}
          onChange={setAddress}
          description="As the Devices page shows it, for example 192.168.1.20:8761."
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Text
          label="Pairing code"
          value={code}
          onChange={setCode}
          onEnter={pair}
          description="8 letters and digits, like K7QX-4MPA. Good once, for 15 minutes."
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SafeButton primary disabled={busy} onClick={pair}>
          Pair with the server
        </SafeButton>
      </PanelSectionRow>
      {note ? (
        <PanelSectionRow>
          <Field label={note} bottomSeparator="none" />
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
};

const SyncPage: FC = () => {
  const [values, setValues] = useState<SyncSettings | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [note, setNote] = useState("");
  const [store, setStore] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const answer = await storeStatus();
      if (answer?.configured) setStore(answer.store || "the store");
    })();
  }, []);

  const field = (key: keyof SyncSettings) => (text: string) =>
    setValues((old) => (old ? { ...old, [key]: text } : old));

  useEffect(() => {
    (async () => {
      setValues(await call<SyncSettings>("sync_settings"));
      const answer = await call<any>("status");
      if (answer?.steps) setSteps(answer.steps);
    })();
  }, []);

  const save = async () => {
    if (!values) return;
    setNote("Saving ...");
    const answer = await call<any>("save_sync", values);
    if (!answer?.ok) {
      setNote(answer?.error || "Could not save.");
      return;
    }
    setSteps(answer.steps || []);
    setNote("Saved.");
  };

  if (store) {
    // Saves go to the store now. The Syncthing settings would only invite
    // someone to fix a path nothing uses.
    return (
      <>
        <PanelSection title="Server">
          <PanelSectionRow>
            <div>
              Saves go to {store}. The store is shown on the main panel.
            </div>
          </PanelSectionRow>
        </PanelSection>
        <PairSection
          title="Move to another server"
          intro="Only to switch servers. Add this device on the new server's Devices page first."
          onPaired={setStore}
        />
      </>
    );
  }

  const pairing = (
    <PairSection
      title="Connect to your BlockSlot server"
      intro="On the server's web page, open Devices and add this device. It shows the address and a pairing code to enter here."
      onPaired={setStore}
    />
  );

  if (!values) return pairing;

  return (
    <>
      {pairing}
      <PanelSection title="Or: an older Syncthing setup">
        {steps.map((step) => (
          <PanelSectionRow key={step.label}>
            <State ok={step.ok} label={step.label} detail={step.detail} />
          </PanelSectionRow>
        ))}
      </PanelSection>
      <PanelSection title="Syncthing">
        <PanelSectionRow>
          <Text
            label="Syncthing address"
            value={values.url}
            onChange={field("url")}
            onEnter={save}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Text
            label="API key"
            value={values.apikey}
            onChange={field("apikey")}
            password
            description="Syncthing shows this under Actions, Settings."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Text label="Folder id" value={values.folder} onChange={field("folder")} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Text label="Hub name" value={values.hub_name} onChange={field("hub_name")} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Text label="Hub device id" value={values.hub_id} onChange={field("hub_id")} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Text
            label="This device's folder inside the share"
            value={values.device_dir}
            onChange={field("device_dir")}
            description="Each device owns one folder in the shared folder."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Focusable style={{ display: "flex", gap: "8px" }}>
            <SafeButton primary onClick={save}>
              Save and test
            </SafeButton>
          </Focusable>
        </PanelSectionRow>
        {note ? (
          <PanelSectionRow>
            <Field label={note} bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
};

// --------------------------------------------------------- emulator games

/** How a tree reads in the picker: a library, or one game and its emulator. */
function treeName(row: SaveSet): string {
  if (row.kind === "game") {
    return (row.one_game || row.name) + "   emulator game" + (row.label ? ", " + row.label : "");
  }
  return row.name + "   emulator library";
}

/** The most rows the page asks for. A library can hold two thousand games. */
const GAME_LIMIT = 200;

/**
 * Pick one save of a game two devices both played. One button per save; the
 * other save stays on the store as history.
 */
const ChooseSave: FC<{
  game: LibraryGame;
  choices: GameChoice[];
  onPick: (choice: GameChoice) => void;
  closeModal?: () => void;
}> = ({ game, choices, onPick, closeModal }) => (
  <ConfirmModal
    strTitle={"Which save of " + game.title + " do you keep playing?"}
    strDescription={
      "Both are real play. Every device uses the one you pick from its next " +
      "launch, and the other stays on the store as history, so nothing is deleted."
    }
    bAlertDialog={true}
    strOKButtonText="Cancel"
    onOK={() => closeModal?.()}
  >
    <Focusable style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
      {choices.map((choice) => (
        <DialogButton
          key={choice.id}
          onClick={() => {
            closeModal?.();
            onPick(choice);
          }}
        >
          {"Use the " + choice.device + " save, " + when(choice.when)}
        </DialogButton>
      ))}
    </Focusable>
  </ConfirmModal>
);

/** The games of one emulator library or the one emulator game, newest first. */
const TreeGames: FC<{ tree: SaveSet }> = ({ tree }) => {
  const [search, setSearch] = useState("");
  const [answer, setAnswer] = useState<LibraryGames | null>(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  // Only the newest request may draw: an older one can answer last.
  const asked = useRef(0);

  const load = async (text: string, refresh = false) => {
    const mine = ++asked.current;
    setBusy("Reading the store ...");
    const got = await libraryGames(tree.name, text, GAME_LIMIT, refresh);
    if (mine !== asked.current) return;
    setAnswer(got);
    setBusy("");
  };

  useEffect(() => {
    setAnswer(null);
    setNote("");
    setSearch("");
    load("");
  }, [tree.name]);

  // Typing filters the list the backend already holds, a moment after the
  // last key, so each letter is not a trip to the backend.
  useEffect(() => {
    if (answer === null) return;
    const timer = setTimeout(() => load(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const choose = (game: LibraryGame, choices: GameChoice[]) =>
    showModal(
      <ChooseSave
        game={game}
        choices={choices}
        onPick={async (choice) => {
          setNote("Recording your choice ...");
          const got = await storeChoose(game.name, choice.id);
          setNote(
            got?.ok
              ? game.title + " now uses the " + choice.device + " save."
              : got?.error || "Could not record the choice."
          );
          await load(search, true);
        }}
      />
    );

  const games = answer?.games || [];
  const single = tree.kind === "game";

  return (
    <PanelSection title={single ? "The game" : "Games in " + tree.name}>
      {!single ? (
        <PanelSectionRow>
          <Text label="Find a game" value={search} onChange={setSearch} />
        </PanelSectionRow>
      ) : null}
      {busy ? (
        <PanelSectionRow>
          <Field label={busy} bottomSeparator="none" />
        </PanelSectionRow>
      ) : null}
      {answer?.error ? (
        <PanelSectionRow>
          <State ok={false} label="Could not read the store" detail={answer.error} />
        </PanelSectionRow>
      ) : null}
      {note ? (
        <PanelSectionRow>
          <Field label={note} bottomSeparator="none" />
        </PanelSectionRow>
      ) : null}
      {games.map((game) => (
        <PanelSectionRow key={game.name}>
          <Field
            label={
              <span>
                {game.title}
                {game.two ? (
                  <span style={{ color: WARN, marginLeft: "10px" }}>two saves</span>
                ) : null}
              </span>
            }
            description={[game.label, game.line].filter((part) => part).join("   ")}
            bottomSeparator={game.two && game.choices ? "none" : "standard"}
          />
          {game.two && game.choices ? (
            <Focusable style={{ display: "flex", gap: "8px", paddingBottom: "8px" }}>
              <SafeButton onClick={() => choose(game, game.choices!)}>
                Choose which save to keep
              </SafeButton>
            </Focusable>
          ) : null}
        </PanelSectionRow>
      ))}
      {answer && !answer.error && !busy && games.length === 0 ? (
        <PanelSectionRow>
          <Field
            label={search.trim() ? "No game matches that" : "Nothing saved yet"}
            description={
              search.trim()
                ? undefined
                : "A game shows here once a device has uploaded its save."
            }
          />
        </PanelSectionRow>
      ) : null}
      {answer && answer.total > games.length ? (
        <PanelSectionRow>
          <Field
            label={"Showing " + games.length + " of " + answer.total}
            description="Type part of a name to find the rest."
            bottomSeparator="none"
          />
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
};

const EmulatorPage: FC = () => {
  const [sets, setSets] = useState<SaveSet[]>([]);
  const [picked, setPicked] = useState<string>("");
  const [folder, setFolder] = useState("");
  const [name, setName] = useState("");
  const [everyFile, setEveryFile] = useState(false);
  const [note, setNote] = useState("");

  const load = async () => {
    const answer = await call<any>("sets");
    const rows: SaveSet[] = answer?.sets || [];
    setSets(rows);
    if (rows.length && !rows.some((row) => row.name === picked)) {
      setPicked(rows[0].name);
      setFolder(rows[0].root);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const current = sets.find((row) => row.name === picked);
  const what = current?.kind === "game" ? "game" : "library";

  return (
    <>
      <PanelSection title="Emulator games">
        <PanelSectionRow>
          <Field
            label="What these are"
            description={
              "Every game is one save. An emulator library is one emulator's " +
              "saves folder, split into one save per game. An emulator game is " +
              "one game with a folder of its own. Each needs the folder its " +
              "saves live in on THIS device. Steam games need none of this."
            }
          />
        </PanelSectionRow>
        {sets.length ? (
          <PanelSectionRow>
            <Dropdown
              rgOptions={sets.map((row) => ({
                data: row.name,
                label: treeName(row) + (row.root ? "" : "   no folder here"),
              }))}
              selectedOption={picked}
              onChange={(option: any) => {
                setPicked(option.data);
                setFolder(
                  sets.find((row) => row.name === option.data)?.root || ""
                );
                setNote("");
              }}
            />
          </PanelSectionRow>
        ) : (
          <PanelSectionRow>
            <Field label="None yet" />
          </PanelSectionRow>
        )}
        {current ? (
          <>
            <PanelSectionRow>
              <State
                ok={!!current.root}
                label={
                  current.root
                    ? "Ready on this device"
                    : "This device has no folder for it"
                }
                detail={
                  current.devices +
                  " device(s) know this " +
                  what +
                  (current.label ? ". Saves owned by " + current.label + "." : "")
                }
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Text
                label="Its folder on this device"
                value={folder}
                onChange={setFolder}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Focusable style={{ display: "flex", gap: "8px" }}>
                <SafeButton
                  primary
                  onClick={async () => {
                    const answer = await call<any>("set_root", picked, folder);
                    setNote(
                      answer?.ok
                        ? answer.exists
                          ? "Saved."
                          : "Saved, but that folder is not there yet."
                        : answer?.error || "Could not save."
                    );
                    load();
                  }}
                >
                  Save the folder
                </SafeButton>
                <SafeButton
                  onClick={() =>
                    showModal(
                      <ConfirmModal
                        strTitle={"Remove " + picked + "?"}
                        strDescription={
                          "No save file is touched and no other device changes. " +
                          "Any game using it stops syncing until it is given another."
                        }
                        strOKButtonText="Remove it"
                        onOK={async () => {
                          await call("remove_set", picked);
                          setPicked("");
                          load();
                        }}
                      />
                    )
                  }
                >
                  Remove
                </SafeButton>
              </Focusable>
            </PanelSectionRow>
          </>
        ) : null}
        {note ? (
          <PanelSectionRow>
            <Field label={note} bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      {current ? <TreeGames tree={current} /> : null}

      <PanelSection title="New emulator library">
        <PanelSectionRow>
          <Text label="Name" value={name} onChange={setName} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Carry every file in the folder"
            description={
              "On: every file in the folder is save data, which is what a " +
              "console emulator needs. Off: only files that look like saves " +
              "are carried."
            }
            checked={everyFile}
            onChange={setEveryFile}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Focusable>
            <SafeButton
              disabled={!name.trim()}
              onClick={async () => {
                const answer = await call<any>("add_set", name, everyFile);
                setNote(answer?.ok ? "Added " + name + "." : answer?.error || "");
                if (answer?.ok) {
                  setPicked(name.trim());
                  setName("");
                  load();
                }
              }}
            >
              Add it
            </SafeButton>
          </Focusable>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};

// --------------------------------------------------------------- settings

const SettingsPage: FC = () => {
  const [state, setState] = useState<EngineState | null>(null);
  const [device, setDevice] = useState("");
  const [note, setNote] = useState("");

  const load = async () => {
    const answer = await call<EngineState>("engine_state");
    setState(answer);
    return answer;
  };

  useEffect(() => {
    load().then((answer) => setDevice(answer?.device_name || ""));
  }, []);

  if (!state) return <PanelSection title="Settings" />;

  return (
    <>
      <PanelSection title="This device">
        <PanelSectionRow>
          <Text
            label="What to call this device"
            value={device}
            onChange={setDevice}
            description="What the other devices call it when they report a save."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Focusable>
            <SafeButton
              primary
              onClick={async () => {
                const answer = await call<any>("set_device_name", device);
                setNote(answer?.ok ? "Saved." : answer?.error || "Could not save.");
              }}
            >
              Save
            </SafeButton>
          </Focusable>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="The engine">
        <PanelSectionRow>
          <State
            ok={state.installed && state.current}
            label={
              !state.installed
                ? "Not installed. Nothing syncs until it is."
                : state.current
                ? "Installed and up to date"
                : "Installed, but older than this plugin's copy"
            }
            detail={state.engine}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="What it does"
            description={
              "savepick is the part that works. Steam starts it instead of the " +
              "game, it brings the newest save down first, and it backs the save " +
              "up when you quit."
            }
          />
        </PanelSectionRow>
        {state.have_source ? (
          <PanelSectionRow>
            <Focusable>
              <SafeButton
                onClick={async () => {
                  const answer = await call<any>("install_engine");
                  setNote(answer?.ok ? "Engine installed." : answer?.error || "");
                  load();
                }}
              >
                {!state.installed
                  ? "Install it"
                  : state.current
                  ? "Reinstall it"
                  : "Update it"}
              </SafeButton>
            </Focusable>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection title="Everything else">
        <PanelSectionRow>
          <State
            ok={state.ludusavi_found}
            label={state.ludusavi_found ? "ludusavi found" : "ludusavi missing"}
            detail={state.ludusavi}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="What it does"
            description={
              "ludusavi knows where games keep their saves and does every copy. " +
              "BlockSlot installs its official 0.31.0 release. That release " +
              "cannot yet match a Windows save path to a Proton one, so a save " +
              "moves between SteamOS devices today, and between SteamOS and " +
              "Windows once ludusavi releases that."
            }
          />
        </PanelSectionRow>
        {!state.ludusavi_found && state.ludusavi_archive ? (
          <PanelSectionRow>
            <Focusable>
              <SafeButton
                primary
                onClick={async () => {
                  setNote("Installing ludusavi ...");
                  const answer = await call<any>("install_ludusavi");
                  setNote(
                    answer?.ok ? "ludusavi installed." : answer?.error || "Could not install it."
                  );
                  load();
                }}
              >
                Install ludusavi
              </SafeButton>
            </Focusable>
          </PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          <Field label="Settings file" description={state.settings_file} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Log" description={state.log} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Steam" description={state.steam || "not found"} />
        </PanelSectionRow>
        {note ? (
          <PanelSectionRow>
            <Field label={note} bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
};

// --------------------------------------------------------------- activity

const ActivityPage: FC = () => {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [where, setWhere] = useState("");

  const load = async () => {
    const answer = await call<any>("log_tail", 120);
    setLines(answer?.lines || []);
    setWhere(answer?.path || "");
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, []);

  return (
    <PanelSection title="Activity">
      <PanelSectionRow>
        <Field
          label={where || "no log yet"}
          description="What the engine did, in its own words."
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Focusable
          style={{
            maxHeight: "60vh",
            overflowY: "scroll",
            fontFamily: "monospace",
            fontSize: "12px",
            lineHeight: "1.5",
            background: "#0e1117",
            padding: "8px",
            borderRadius: "4px",
          }}
        >
          {lines.length === 0 ? (
            <div>Nothing yet. The log appears the first time a game starts.</div>
          ) : (
            lines.map((line, index) => (
              <div key={index} style={{ color: COLOUR[line.kind] || "#c7d1db" }}>
                {line.text}
              </div>
            ))
          )}
        </Focusable>
      </PanelSectionRow>
    </PanelSection>
  );
};

/** The backend says what kind of line it is. This only picks the colour. */
const COLOUR: Record<string, string> = { bad: BAD, warn: WARN, good: GOOD };

// ------------------------------------------------------------------- page

export const BlockslotPage: FC = () => (
  <SidebarNavigation
    title="BlockSlot"
    showTitle
    pages={[
      { title: "Games", content: <GamesPage />, route: "/blockslot/games" },
      { title: "Server", content: <SyncPage />, route: "/blockslot/sync" },
      { title: "Emulator games", content: <EmulatorPage />, route: "/blockslot/sets" },
      { title: "Settings", content: <SettingsPage />, route: "/blockslot/settings" },
      { title: "Activity", content: <ActivityPage />, route: "/blockslot/activity" },
    ]}
  />
);
