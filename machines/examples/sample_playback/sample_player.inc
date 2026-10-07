; Shared sample-import demonstration. Only public MDS imports are used.
;
; The wrapper defines BANK_ID (0 Internal, 1 Userwave, 2 RAM slots, -1
; parameter), REVERSE (0/1), and parameter offsets. SLOT and BANK are latched at TRIG;
; DEC and LEV are live controls. Length and playback step are latched at TRIG.
; Playback is one-shot, at the source's native rate, with nearest-neighbour
; sampling.
;
; Two firmware calls do the sample access:
;   TRIG_INFO:    SAMPLE_INFO(bank, slot) -> length, native step, loop start
;   READ_SAMPLES: SAMPLE_READ(bank, slot, positions) -> linear PCM
; The zero JSR operands below are import placeholders.
; Each example's JSON program section names the operands; the firmware loader
; fills in their addresses at installation.
;
; Persistent Y state (all values owned by this track):
; +9 integer source position, +10 fractional position (24 bits),
; +11 decay envelope, +12 active, +13 logical bank, +14 logical slot,
; +15 latched length, +16 integer step, +17 fractional step (24 bits).

        section SAMPLE_DEMO
INIT:
        movec   #>$ffffff,m6
        clr     a
        move    a,y:(r6+9)
        move    a,y:(r6+10)
        move    a,y:(r6+11)
        move    a,y:(r6+12)
        move    a,y:(r6+13)
        move    a,y:(r6+14)
        move    a,y:(r6+15)
        move    a,y:(r6+16)
        move    a,y:(r6+17)
        rts

TRIG:
        movec   #>$ffffff,m6
        if      BANK_ID<0
        move    y:(r6+P_BANK),x0
        else
        move    #>BANK_ID,x0
        endif
        move    y:(r6+P_SLOT),y0
        move    x0,y:(r6+13)
        move    y0,y:(r6+14)
TRIG_INFO:
        ; SAMPLE_INFO input: X0 = bank (0 Internal, 1 UW, 2 RAM), Y0 = slot (0-based).
        ; Output: A1 = length (0 unavailable), B1 = native Q16.16 step,
        ; X1 = loop start ($ffffff if none). This one-shot ignores the loop.
        jsr     >$000000         ; kind 5 / SAMPLE_INFO, zero addend
        tst     a               ; A1 = length, B1 = native Q16.16 step
        beq     <TRIG_EMPTY
        tst     b
        beq     <TRIG_EMPTY      ; no usable playback rate
        move    a1,y:(r6+15)
        ; Convert once per voice, not once per block. Preserve A1=length.
        move    b1,x0
        lsr     #16,b
        move    b1,y:(r6+16)
        move    x0,b
        lsl     #8,b
        move    b1,y:(r6+17)
        move    a1,x0
        move    y:(r6+P_START),y0
        mpy     x0,y0,a         ; floor(length * STRT), STRT is unipolar Q23
        if      REVERSE
        move    a1,y0
        move    x0,a
        sub     #1,a
        sub     y0,a            ; offset backwards from the last source frame
        endif
        move    a1,y:(r6+9)
        clr     a
        move    a,y:(r6+10)
        move    #>$7fffff,a
        move    a,y:(r6+11)
        move    #>1,a
        move    a,y:(r6+12)
        rts
TRIG_EMPTY:
        clr     a
        move    a,y:(r6+12)      ; unavailable slot remains silent
        move    a,y:(r6+15)
        move    a,y:(r6+16)
        move    a,y:(r6+17)
        rts

RENDER:
        movec   #>$ffffff,m6
        move    y:(r6+12),a
        tst     a
        beq     >SILENCE
        move    y:(r6+15),y1     ; length belongs to this triggered voice
        move    y:(r6+9),a
        tst     a
        bmi     >SILENCE
        cmp     y1,a
        bge     >SILENCE         ; finished at the latched boundary

        ; X1:X0 is the cached 48-bit step; A1:A0 is the source position.
        move    y:(r6+16),x1
        move    y:(r6+17),x0
        move    y:(r6+10),a0
        move    #>$ffffff,y0
POSITIONS:
        ; Import the temporary Y buffer. Fill it with integer source indices;
        ; these are positions within the sample, never storage addresses.
        move    #>$000000,r0    ; kind 4 / TEMP_Y
        movec   #>$ffffff,m0
POSITION_LOOP:
        do      #32,POSITION_END
        if      REVERSE
        move    a1,y:(r0)+      ; negative positions are silent in SAMPLE_READ
        sub     x,a
        else
        ; Keep the final block inside the latched voice length even if the
        ; live slot has since grown. SAMPLE_READ also bounds the live slot.
        move    a1,b
        cmp     y1,a
        tfr     y0,b ifge
        move    b1,y:(r0)+
        add     x,a
        endif
POSITION_END:
        move    a1,y:(r6+9)
        move    a0,y:(r6+10)
        move    y:(r6+13),x0    ; SAMPLE_READ preserves these two selectors
        move    y:(r6+14),y0
        move    #32,n0
READ_SAMPLES:
        ; SAMPLE_READ input: X0/Y0 = bank/slot, N0 = requested count (1..32),
        ; temporary Y = integer source positions. Output: temporary X holds
        ; a complete 32-word signed Q23 PCM block.
        ; A1 = 1 for an available slot, 0 otherwise. Invalid positions and
        ; words beyond N0 are zero.
        jsr     >$000000         ; kind 5 / SAMPLE_READ; out-of-range -> zero
        tst     a
        beq     >SILENCE

        ; The service clobbers working registers. Reload the output gain,
        ; envelope and PCM pointer; R6/R7 and their modulo state survive.
PCM:
        move    #>$000000,r0    ; kind 3 / TEMP_X
        movec   #>$ffffff,m0
        move    y:(r6+11),y1
        move    y:(r6+P_DEC),x1
        move    y:(r6+P_LEVEL),y0
OUTPUT_LOOP:
        do      #32,OUTPUT_END
        move    x:(r0)+,x0
        mpy     x0,y1,a
        move    a1,x0
        mpy     x0,y0,a
        move    a1,y:(r7)+
        mpy     x1,y1,a
        move    a1,y1
OUTPUT_END:
        move    y1,y:(r6+11)
        rts

SILENCE:
        clr     a
        move    a,y:(r6+12)
        rep     #32
        move    a,y:(r7)+       ; every path writes the whole output block
        rts
        endsec
