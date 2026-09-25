/**
 * Blockslot in the Quick Access menu, and the full page behind it.
 *
 * The panel keeps only what you want mid-session: is sync working, and turn it
 * on for the game you are about to play. Everything the desktop window has
 * lives on the full page, which the panel opens.
 *
 * Every write of a launch option goes through Steam, never through the file.
 * Steam holds localconfig.vdf in memory and writes it out on exit, so an edit
 * to the file from inside Steam is thrown away. See `api.ts`.
 */

import { definePlugin, routerHook } from "@decky/api";
import {
  ButtonItem,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { useEffect, useState, FC } from "react";

import { call, Game, Step } from "./api";
import {
  BlockslotPage,
  GameRows,
  loadWithHub,
  StoreSection,
  useSyncToggle,
} from "./pages";

const ROUTE = "/blockslot";

const Panel: FC = () => {
  const [games, setGames] = useState<Game[]>([]);
  const [steps, setSteps] = useState<Step[]>([]);
  const [device, setDevice] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const flip = useSyncToggle(setGames, setNote);

  const loadGames = async () => {
    const answer = await call<any>("games");
    if (answer?.games) setGames(answer.games);
  };

  const loadStatus = async () => {
    const answer = await call<any>("status");
    if (answer) {
      setSteps(answer.steps || []);
      setDevice(answer.device || "");
    }
  };

  useEffect(() => {
    (async () => {
      setBusy("Reading your library ...");
      await loadStatus();
      await loadWithHub(loadGames, setBusy);
    })();
  }, []);

  const broken = steps.filter((step) => !step.ok);

  return (
    <>
      <PanelSection title={device ? "This device: " + device : "BlockSlot"}>
        {busy ? (
          <PanelSectionRow>
            <Field label={busy} />
          </PanelSectionRow>
        ) : null}
        {!busy && broken.length === 0 ? (
          <PanelSectionRow>
            <Field label="Sync is working" bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
        {broken.map((step) => (
          <PanelSectionRow key={step.label}>
            <Field label={step.label} description={step.detail} />
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              Navigation.CloseSideMenus();
              Navigation.Navigate(ROUTE);
            }}
          >
            Open BlockSlot
          </ButtonItem>
        </PanelSectionRow>
        {note ? (
          <PanelSectionRow>
            <Field label={note} bottomSeparator="none" />
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <StoreSection />

      <PanelSection title="Games Steam Cloud does not cover">
        <GameRows games={games} busy={busy} flip={flip} />
      </PanelSection>
    </>
  );
};

export default definePlugin(() => {
  routerHook.addRoute(ROUTE, BlockslotPage, { exact: false });
  return {
    name: "Blockslot",
    titleView: <div className={staticClasses.Title}>BlockSlot</div>,
    content: <Panel />,
    icon: <BlockslotIcon />,
    onDismount() {
      routerHook.removeRoute(ROUTE);
    },
  };
});

/** The Blockslot mark in one colour, as Steam draws every plugin icon: the
 * cube between two sync arrows (assets/icon-small.svg), its three faces told
 * apart by opacity instead of by colour. */
function BlockslotIcon() {
  return (
    <svg viewBox="0 0 512 512" width="1em" height="1em" fill="currentColor">
      <defs>
        <marker id="blockslot-arrow" viewBox="0 0 10 10" refX="2" refY="5"
          markerWidth="2.1" markerHeight="2.1" orient="auto">
          <path d="M1 1 L9 5 L1 9 Z" fill="currentColor" />
        </marker>
      </defs>
      <path d="M60 196 A206 206 0 0 1 428 150" fill="none" stroke="currentColor"
        strokeWidth="44" markerEnd="url(#blockslot-arrow)" />
      <path d="M452 316 A206 206 0 0 1 84 362" fill="none" stroke="currentColor"
        strokeWidth="44" markerEnd="url(#blockslot-arrow)" />
      <circle cx="60" cy="196" r="22" />
      <circle cx="452" cy="316" r="22" />
      <polygon points="256,120 376,190 256,260 136,190" />
      <polygon points="136,190 256,260 256,400 136,330" opacity="0.7" />
      <polygon points="256,260 376,190 376,330 256,400" opacity="0.45" />
    </svg>
  );
}
