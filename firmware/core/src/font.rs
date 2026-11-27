//! Bitmap fonts.

/// One character. Each entry of `rows` holds one pixel row, right-aligned:
/// bit `width - 1` is the leftmost column.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Glyph {
    pub ch: char,
    pub width: u8,
    pub rows: [u8; 5],
}

/// A fixed-height bitmap font.
pub struct Font {
    pub glyphs: &'static [Glyph],
    /// Empty columns between two glyphs.
    pub spacing: u8,
    pub height: u8,
}

impl Font {
    /// Looks up a glyph. Lowercase letters fall back to uppercase.
    pub fn glyph(&self, ch: char) -> Option<&Glyph> {
        let ch = ch.to_ascii_uppercase();
        self.glyphs.iter().find(|g| g.ch == ch)
    }

    /// Width of `text` in pixels, including the spacing between glyphs.
    pub fn text_width(&self, text: &str) -> usize {
        let (sum, count) = text
            .chars()
            .filter_map(|c| self.glyph(c))
            .fold((0usize, 0usize), |(sum, n), g| {
                (sum + g.width as usize, n + 1)
            });
        sum + count.saturating_sub(1) * self.spacing as usize
    }
}

const fn g(ch: char, width: u8, rows: [u8; 5]) -> Glyph {
    Glyph { ch, width, rows }
}

/// 3×5 pixel font: digits, uppercase letters and the symbols the clock needs.
pub const FONT_3X5: Font = Font {
    spacing: 1,
    height: 5,
    glyphs: &[
        g('0', 3, [0b111, 0b101, 0b101, 0b101, 0b111]),
        g('1', 3, [0b010, 0b110, 0b010, 0b010, 0b111]),
        g('2', 3, [0b111, 0b001, 0b111, 0b100, 0b111]),
        g('3', 3, [0b111, 0b001, 0b111, 0b001, 0b111]),
        g('4', 3, [0b101, 0b101, 0b111, 0b001, 0b001]),
        g('5', 3, [0b111, 0b100, 0b111, 0b001, 0b111]),
        g('6', 3, [0b111, 0b100, 0b111, 0b101, 0b111]),
        g('7', 3, [0b111, 0b001, 0b010, 0b010, 0b010]),
        g('8', 3, [0b111, 0b101, 0b111, 0b101, 0b111]),
        g('9', 3, [0b111, 0b101, 0b111, 0b001, 0b111]),
        g(':', 1, [0b0, 0b1, 0b0, 0b1, 0b0]),
        // A single dot at the top, e.g. to mark an unsynced time.
        g('*', 1, [0b1, 0b0, 0b0, 0b0, 0b0]),
        g('-', 3, [0b000, 0b000, 0b111, 0b000, 0b000]),
        g('°', 3, [0b011, 0b011, 0b000, 0b000, 0b000]),
        g(' ', 2, [0b00, 0b00, 0b00, 0b00, 0b00]),
        g('A', 3, [0b111, 0b101, 0b111, 0b101, 0b101]),
        g('B', 3, [0b111, 0b101, 0b111, 0b101, 0b111]),
        g('C', 3, [0b111, 0b100, 0b100, 0b100, 0b111]),
        g('D', 3, [0b110, 0b101, 0b101, 0b101, 0b110]),
        g('E', 3, [0b111, 0b100, 0b111, 0b100, 0b111]),
        g('F', 3, [0b111, 0b100, 0b111, 0b100, 0b100]),
        g('G', 3, [0b111, 0b100, 0b101, 0b101, 0b111]),
        g('H', 3, [0b101, 0b101, 0b111, 0b101, 0b101]),
        g('I', 3, [0b111, 0b010, 0b010, 0b010, 0b111]),
        g('J', 3, [0b001, 0b001, 0b001, 0b101, 0b111]),
        g('K', 3, [0b101, 0b110, 0b100, 0b110, 0b101]),
        g('L', 3, [0b100, 0b100, 0b100, 0b100, 0b111]),
        g('M', 3, [0b101, 0b111, 0b111, 0b101, 0b101]),
        g('N', 3, [0b101, 0b111, 0b111, 0b111, 0b101]),
        g('O', 3, [0b010, 0b101, 0b101, 0b101, 0b010]),
        g('P', 3, [0b111, 0b101, 0b111, 0b100, 0b100]),
        g('Q', 3, [0b010, 0b101, 0b101, 0b111, 0b011]),
        g('R', 3, [0b111, 0b101, 0b111, 0b110, 0b101]),
        g('S', 3, [0b111, 0b100, 0b111, 0b001, 0b111]),
        g('T', 3, [0b111, 0b010, 0b010, 0b010, 0b010]),
        g('U', 3, [0b101, 0b101, 0b101, 0b101, 0b111]),
        g('V', 3, [0b101, 0b101, 0b101, 0b101, 0b010]),
        g('W', 3, [0b101, 0b101, 0b111, 0b111, 0b101]),
        g('X', 3, [0b101, 0b101, 0b010, 0b101, 0b101]),
        g('Y', 3, [0b101, 0b101, 0b010, 0b010, 0b010]),
        g('Z', 3, [0b111, 0b001, 0b010, 0b100, 0b111]),
    ],
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glyph_rows_fit_their_width() {
        for g in FONT_3X5.glyphs {
            for row in g.rows {
                assert!(
                    row >> g.width == 0,
                    "glyph {:?} is wider than {}",
                    g.ch,
                    g.width
                );
            }
        }
    }

    #[test]
    fn no_duplicate_glyphs() {
        for (i, a) in FONT_3X5.glyphs.iter().enumerate() {
            assert!(
                !FONT_3X5.glyphs[i + 1..].iter().any(|b| b.ch == a.ch),
                "{:?}",
                a.ch
            );
        }
    }

    #[test]
    fn text_width() {
        assert_eq!(FONT_3X5.text_width(""), 0);
        assert_eq!(FONT_3X5.text_width("1"), 3);
        assert_eq!(FONT_3X5.text_width("12:34"), 17);
        assert_eq!(FONT_3X5.text_width("-5°C"), 15);
        // unknown characters take no space
        assert_eq!(FONT_3X5.text_width("1~1"), 7);
    }
}
