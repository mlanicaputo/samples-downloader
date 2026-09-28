from pytubefix import YouTube
from pytubefix.cli import on_progress

# url = "https://www.youtube.com/watch?v=hZG1pRKVhDs"
# url = "https://www.youtube.com/watch?v=fAZg-W404fU"
# url = "https://www.youtube.com/watch?v=lZbfNtDCHdM"
# url = "https://www.youtube.com/watch?v=zejIhEP2IMc"
# url = "https://www.youtube.com/watch?v=mLqQqqLc0TA"
url = "https://www.youtube.com/watch?v=aOYHN42AjIc"

yt = YouTube(url, on_progress_callback=on_progress)
print(yt.title)

ys = yt.streams.get_audio_only()
# ys.download()
print("it worked")


# from pytubefix import YouTube
# from moviepy import *

# def download_youtube_section(url, start_time, end_time, output_filename="clip.mp4"):
#     yt = YouTube(url)
#     stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
#     stream.download(filename="temp.mp4")

#     clip = VideoFileClip("temp.mp4").subclipped(start_time, end_time)
#     clip.write_videofile(output_filename, codec="libx264", audio_codec="aac")
#     print(f"Saved section as {output_filename}")

# # Example
# download_youtube_section("https://www.youtube.com/watch?v=dy27BMTEEeU", 2608, 2690)


