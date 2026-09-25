"""Tests for saveunits: which game each save file belongs to.

Every path here is a real one from RetroBat on DESKTOP or RetroDECK on the
Deck, measured on 2026-09-24.

    python3 -m unittest test_saveunits
"""

import unittest

import saveunits as su

ALIASES = [["gc", "gamecube"], ["mame", "mame-sa"], ["3ds", "n3ds"],
           ["sg-1000", "sg1000"]]


def unit(rel):
    return su.unit_of(rel, ALIASES)


class OneFilePerGame(unittest.TestCase):
    def test_a_battery_save(self):
        self.assertEqual(unit("snes/ABC Monday Night Football (U).srm"),
                         ("snes", "ABC Monday Night Football (U)"))

    def test_an_inner_dot_stays(self):
        self.assertEqual(unit("gba/Super Mario Advance 4 - Super Mario Bros. 3.srm"),
                         ("gba", "Super Mario Advance 4 - Super Mario Bros. 3"))

    def test_a_memory_card_slot_joins_its_game(self):
        self.assertEqual(unit("psx/007 - The World Is Not Enough (USA).1.mcr"),
                         unit("psx/007 - The World Is Not Enough (USA).srm"))

    def test_aliases_make_one_system(self):
        self.assertEqual(unit("sg-1000/Ys - The Vanished Omens (UE) [!].srm"),
                         unit("sg1000/Ys - The Vanished Omens (UE) [!].srm"))


class NestedLayouts(unittest.TestCase):
    def test_per_game_folders(self):
        self.assertEqual(unit("3do/opera/per_game/Night Trap (USA) (Disc 2).0.srm"),
                         ("3do", "Night Trap (USA) (Disc 2)"))

    def test_ps2_memory_cards(self):
        self.assertEqual(unit("ps2/pcsx2/memcards/BloodRayne (USA).ps2"),
                         ("ps2", "BloodRayne (USA)"))

    def test_a_game_id_folder(self):
        self.assertEqual(unit("psp/PPSSPP-SA/ULES01248Diabolik/SAVE1.SAV"), ("psp", "ULES01248"))
        self.assertEqual(unit("psp/PPSSPP-SA/ULES01248Diabolik/SAVE4.SAV"), ("psp", "ULES01248"))
        self.assertEqual(unit("ps3/rpcs3/NPUA80856/PROFILE.SAV"), ("ps3", "NPUA80856"))

    def test_gamecube_by_disc_code(self):
        self.assertEqual(unit("dolphin/User/GC/USA/Card A/70-GIKE-ikaruga_save_data.gci"),
                         ("dolphin", "GIKE"))
        self.assertEqual(unit("gamecube/dolphin/US/Card A/01-GPIE-Pikmin dataFile.gci"),
                         unit("gc/dolphin/US/Card A/01-GPIE-Pikmin dataFile.gci"))

    def test_mame_by_rom(self):
        self.assertEqual(unit("mame/mame2003-plus/hi/005.hi"), ("mame", "005"))
        self.assertEqual(unit("mame-sa/mame2003-plus/hi/005.hi"), ("mame", "005"))

    def test_a_3ds_title(self):
        rel = ("3ds/Citra/sdmc/Nintendo 3DS/00000000000000000000000000000000/"
               "00000000000000000000000000000000/title/00040000/0017BB00/data/00000001/main")
        self.assertEqual(unit(rel), ("3ds", "0017bb00"))

    def test_a_wii_u_title(self):
        self.assertEqual(unit("wiiu/cemu/00050000/10143500/user/80000001/cking.sav"),
                         ("wiiu", "10143500"))


class SharedAndContent(unittest.TestCase):
    def test_shared_memory_is_its_own_save(self):
        self.assertEqual(unit("segacd/scd_U.brm"), ("segacd", "shared memory"))
        self.assertEqual(unit("segacd/4Mbit_cart.brm"), ("segacd", "shared memory"))
        self.assertEqual(unit("saturn/mednafen_saturn_libretro_shared.smpc"),
                         ("saturn", "shared memory"))

    def test_a_content_folder_is_one_game(self):
        self.assertEqual(unit("After Burner III (USA).m3u/scd_U.brm"),
                         ("", "After Burner III (USA)"))
        self.assertEqual(unit("Skeleton Warriors(US)/Skeleton Warriors.bcr"),
                         ("", "Skeleton Warriors(US)"))

    def test_an_unknown_structure_is_one_save_per_emulator(self):
        system, name = unit("switch/ryujinx/nand/user/save/0000000000000003/1/Mario64/Mario64.sav")
        self.assertEqual(system, "switch")
        self.assertTrue(name.endswith("(all saves)"))


class Labels(unittest.TestCase):
    def test_the_emulator_owns_the_save(self):
        self.assertEqual(su.label_for("snes"), "RetroArch (SNES)")
        self.assertEqual(su.label_for("ps2"), "PCSX2")
        self.assertEqual(su.label_for("gc"), "Dolphin")
        self.assertEqual(su.label_for(""), "RetroArch")

    def test_display_names(self):
        self.assertEqual(su.display("snes", "Super Mario World (USA)"),
                         "Super Mario World (USA) (SNES)")
        self.assertEqual(su.display("", "Snatcher"), "Snatcher")


if __name__ == "__main__":
    unittest.main(verbosity=2)
