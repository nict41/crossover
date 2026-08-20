"""Quick profile harness for a modest gen run.
Usage: python scripts/profile_run.py
Writes `profile.before.prof` in the repo root.
"""
import cProfile
import pstats
import os
import runpy

OUT = os.path.join(os.getcwd(), "profile.run.prof")

def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    cProfile.runctx("runpy.run_path('tools/gen_pcb_smd.py', run_name='__main__')",
                    globals(), locals(), OUT)
    p = pstats.Stats(OUT)
    p.strip_dirs().sort_stats('cumulative').print_stats(30)

if __name__ == '__main__':
    main()
