# Plant Band for Sonic Pi
#
# Paste this into a Sonic Pi buffer and press Run. It plays by itself, cycling
# through all four parts. Start plant_bridge.py and the plant takes over as soon
# as its first reading arrives. The bridge sends each sensor as a 0-1 value:
#
#   Bend the stem   /plant/bend   sweeps the arpeggio's filter and glides between its notes
#   Pinch a leaf    /plant/pinch  plays the chord progression and arpeggio; firmer is louder
#   Tilt the pot    /plant/tilt   a pad that slowly blooms in; more tilt is fuller and brighter
#   Touch the stem  /plant/touch  kick, clap and hi-hat pattern
#
# Every part shares one tempo and chord progression, so they always fit together.
# Change the settings below and press Run again while it plays.

use_bpm 96
progression = (ring chord(:a3, :minor), chord(:f3, :major), chord(:c3, :major), chord(:g3, :major))

# true: play a 16-bar performance until the plant connects.
# false: stay silent until the plant sends readings.
play_by_itself = true

# Sonic Pi normally schedules sound half a second ahead. A shorter wait makes the
# plant feel more responsive. Raise it if you hear timing glitches.
set_sched_ahead_time! 0.1

# Starting values: every part is quiet until something sets them.
set :bend, 0
set :pinch, 0
set :tilt, 0
set :touch, 0
set :chord, progression[0]
set :plant_heard, false

# ---- Listen for the bridge ----------------------------------------------
# The first reading also tells the self-playing performance below to stop.
live_loop :hear_bend do
  use_real_time
  value = (sync "/osc*/plant/bend")[0]
  set :plant_heard, true
  set :bend, value
end

live_loop :hear_pinch do
  use_real_time
  value = (sync "/osc*/plant/pinch")[0]
  set :plant_heard, true
  set :pinch, value
end

live_loop :hear_tilt do
  use_real_time
  value = (sync "/osc*/plant/tilt")[0]
  set :plant_heard, true
  set :tilt, value
end

live_loop :hear_touch do
  use_real_time
  value = (sync "/osc*/plant/touch")[0]
  set :plant_heard, true
  set :touch, value
end

# ---- Playing by itself ----------------------------------------------------
# Stands in for four players, setting the same values the plant would send.
# Each pass is 16 bars: drums, then the arpeggio, the sweep, the bloom,
# everything together, and a quiet ending. It stops once the plant connects.
live_loop :self_play do
  stop if get[:plant_heard] || !play_by_itself
  bar = tick % 16
  set :touch, (bar < 12 ? 1 : 0)
  set :pinch, (bar < 2 ? 0 : bar < 4 ? 0.5 : bar < 14 ? 0.8 : 0)
  set :tilt, (bar < 6 ? 0 : bar < 12 ? 0.7 : 0.9 - 0.2 * (bar - 12))
  16.times do |step|
    stop if get[:plant_heard]
    # The stem bends and springs back across each bar from bar 5 to bar 12.
    set :bend, ((4..11).include?(bar) ? Math.sin(step * Math::PI / 16) : 0)
    sleep 0.25
  end
end

# ---- Shared chord clock: one chord per bar ------------------------------
live_loop :clock do
  set :chord, progression.tick
  sleep 4
end

# ---- Pinch: chord progression and arpeggio, shaped by the bend ----------
live_loop :arpeggio do
  step = tick
  pinch = get[:pinch]
  if pinch > 0
    bend = get[:bend]
    chord_notes = get[:chord]
    pattern = (ring chord_notes[0], chord_notes[1], chord_notes[2], chord_notes[0] + 12,
               chord_notes[2] + 12, chord_notes[0] + 12, chord_notes[2], chord_notes[1])
    note = pattern[step]
    # A firm pinch also sounds the whole chord at the start of each bar.
    if step % 16 == 0 && pinch > 0.4
      synth :prophet, notes: chord_notes, release: 3, cutoff: 80, amp: 0.25 * pinch
    end
    # Bending opens the filter (the sweep). A deeper bend also makes each note
    # start from the previous one and slide up or down to it (the glide).
    glide = bend * 0.2
    voice = synth :tb303, note: (glide > 0.01 ? pattern[step - 1] : note), release: 0.25,
      cutoff: 60 + 65 * bend, res: 0.8, amp: 0.25 + 0.45 * pinch
    control voice, note: note, note_slide: glide if glide > 0.01
  end
  sleep 0.25
end

# ---- Tilt: slow bloom ------------------------------------------------------
with_fx :reverb, room: 0.9, mix: 0.6 do
  live_loop :bloom do
    tilt = get[:tilt]
    # Each bar starts a soft pad on the current chord. Its volume and brightness
    # follow the tilt, sliding over two beats so the sound swells rather than jumps.
    pad = get[:chord].map do |n|
      synth :blade, note: n, attack: 1, sustain: 4, release: 3, amp: 0.3 * tilt,
        cutoff: 70 + 40 * tilt, amp_slide: 2, cutoff_slide: 2
    end
    4.times do
      tilt = get[:tilt]
      pad.each { |voice| control voice, amp: 0.3 * tilt, cutoff: 70 + 40 * tilt }
      sleep 1
    end
  end
end

# ---- Touch: drums ----------------------------------------------------------
live_loop :drums do
  step = tick % 16
  if get[:touch] > 0
    sample :bd_haus, amp: 1.6 if [0, 6, 8].include?(step)
    if [4, 12].include?(step)   # a snap and a snare together stand in for a clap
      sample :perc_snap, amp: 1.2
      sample :elec_hi_snare, amp: 0.5, rate: 1.1
    end
    sample :drum_cymbal_closed, amp: (step.even? ? 0.5 : 0.25)
  end
  sleep 0.25
end
